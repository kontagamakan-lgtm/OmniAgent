"""Routeur multi-LLM et multi-clés, avec basculement à chaud.

Le routeur expose le même contrat que `LLMClient` (`complete`, `abort`,
`close`, `available`) et remplace le client unique quand
`llm_router.enabled = true`. Il gère :

* un pool illimité de clés par fournisseur, les secrets restant dans le coffre
  chiffré (`Vault`) et seules des métadonnées non sensibles étant persistées ;
* quatre états par clé : ACTIVE, COOLDOWN, EXHAUSTED, DEAD ;
* un classement « best-first » par score de puissance, ajusté par les mesures
  réelles de latence, de débit et de réussite ;
* la bascule clé → clé, puis modèle → modèle, et enfin Ollama en dernier
  recours, sans modifier l'historique ReAct (les messages et appels d'outils
  sont convertis au format du fournisseur choisi) ;
* le streaming est volontairement désactivé (`supports_stream = False`) : un
  flux déjà affiché ne peut pas être rejoué sur un autre fournisseur sans
  dupliquer le texte. L'agent utilise donc `complete` et garde la bascule.

Aucune clé n'apparaît dans les journaux, les erreurs ou le fichier d'état.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from config import APP_ID, Settings
from core.errors import LLMError
from core.llm_client import LLMResponse
from core.react_parser import ParsedToolCall, decode_arguments

__all__ = [
    "LLMRouter",
    "ModelSpec",
    "Provider",
    "ProviderFailure",
    "detect_provider",
]

logger = logging.getLogger(f"{APP_ID}.router")

ACTIVE = "ACTIVE"
COOLDOWN = "COOLDOWN"
EXHAUSTED = "EXHAUSTED"
DEAD = "DEAD"
_STATES = {ACTIVE, COOLDOWN, EXHAUSTED, DEAD}
_SERVICE_PREFIX = "llm."
_KEY_ID = re.compile(r"^[0-9a-f]{12}$")
_SECRET_LOOKS_LIKE = re.compile(r"(sk|gsk|AIza)[A-Za-z0-9_\-]{6,}")


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _scrub(text: str) -> str:
    """Retire toute apparence de clé d'une chaîne destinée aux traces."""
    return _SECRET_LOOKS_LIKE.sub("[clé masquée]", str(text or ""))[:300]


@dataclass(frozen=True, slots=True)
class Provider:
    slug: str
    label: str
    base_url: str
    style: str
    prefixes: tuple[str, ...] = ()


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        "openai", "OpenAI", "https://api.openai.com/v1", "openai",
        ("sk-proj-", "sk-"),
    ),
    "anthropic": Provider(
        "anthropic", "Anthropic", "https://api.anthropic.com/v1", "anthropic",
        ("sk-ant-",),
    ),
    "deepseek": Provider(
        "deepseek", "DeepSeek", "https://api.deepseek.com/v1", "openai",
        ("sk-ds-",),
    ),
    "gemini": Provider(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "openai",
        ("AIza",),
    ),
    "groq": Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "openai",
        ("gsk_",),
    ),
    "openrouter": Provider(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "openai",
        ("sk-or-",),
    ),
    "mistral": Provider(
        "mistral", "Mistral", "https://api.mistral.ai/v1", "openai",
        ("sk-mistral-",),
    ),
    "ollama": Provider(
        "ollama", "Ollama (local)", "", "ollama",
    ),
}


def detect_provider(key: str) -> str:
    """Devine le fournisseur d'après le préfixe de la clé.

    `sk-` est ambigu (OpenAI et DeepSeek) : OpenAI est choisi par défaut, et
    l'appelant peut toujours imposer un fournisseur explicite.
    """
    clean = str(key or "").strip()
    for prefix in ("sk-ant-", "sk-or-", "sk-proj-", "sk-ds-", "sk-mistral-"):
        if clean.startswith(prefix):
            for provider in PROVIDERS.values():
                if prefix in provider.prefixes:
                    return provider.slug
            return "openai"
    if clean.startswith("gsk_"):
        return "groq"
    if clean.startswith("AIza"):
        return "gemini"
    if clean.startswith("sk-"):
        return "openai"
    raise ValueError(
        "fournisseur indétectable : préfixe de clé inconnu, choisissez-le explicitement"
    )


@dataclass(frozen=True, slots=True)
class ModelSpec:
    provider: str
    model: str
    label: str
    score: int
    specialties: tuple[str, ...] = field(default_factory=tuple)


#: Matrice de classement initiale. Elle est ajustée ensuite par les mesures
#: réelles (`optimize_scores`), sans jamais dépasser ±8 points.
CATALOGUE: tuple[ModelSpec, ...] = (
    ModelSpec("openai", "gpt-5", "GPT-5", 98, ("reasoning", "coding", "tool_calling")),
    ModelSpec("anthropic", "claude-opus-4-1", "Claude Opus 4.1", 97, ("reasoning", "coding")),
    ModelSpec("openai", "gpt-5-mini", "GPT-5 mini", 92, ("coding", "fast_research", "tool_calling")),
    ModelSpec("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro", 95, ("reasoning", "fast_research")),
    ModelSpec("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5", 94, ("coding", "tool_calling")),
    ModelSpec("deepseek", "deepseek-reasoner", "DeepSeek Reasoner", 90, ("reasoning", "coding")),
    ModelSpec("gemini", "gemini-2.5-flash", "Gemini 2.5 Flash", 88, ("fast_research", "tool_calling")),
    ModelSpec("mistral", "mistral-large-latest", "Mistral Large", 87, ("coding", "tool_calling")),
    ModelSpec("deepseek", "deepseek-chat", "DeepSeek Chat", 86, ("coding", "fast_research")),
    ModelSpec("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B (Groq)", 84, ("fast_research", "tool_calling")),
    ModelSpec("openrouter", "deepseek/deepseek-chat", "DeepSeek Chat (OpenRouter)", 84, ("coding",)),
    ModelSpec("openai", "gpt-4.1-mini", "GPT-4.1 mini", 83, ("fast_research", "tool_calling")),
    ModelSpec("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5", 82, ("fast_research",)),
)


@dataclass(slots=True)
class KeyRecord:
    """Métadonnées d'une clé ; le secret vit uniquement dans le coffre."""

    id: str
    provider: str
    label: str
    hint: str
    status: str = ACTIVE
    cooldown_until: float = 0.0
    exhausted_until: float = 0.0
    last_error: str = ""
    requests: int = 0
    successes: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    speed_tps: float = 0.0
    added_at: str = ""
    last_used_at: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "hint": self.hint,
            "status": self.status,
            "cooldown_until": self.cooldown_until,
            "exhausted_until": self.exhausted_until,
            "last_error": self.last_error,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "speed_tps": self.speed_tps,
            "added_at": self.added_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "KeyRecord":
        provider = str(raw.get("provider") or "").strip().lower()
        status = str(raw.get("status") or ACTIVE).upper()
        return cls(
            id=str(raw.get("id") or ""),
            provider=provider if provider in PROVIDERS else "openai",
            label=str(raw.get("label") or "")[:80],
            hint=str(raw.get("hint") or "")[:24],
            status=status if status in _STATES else ACTIVE,
            cooldown_until=float(raw.get("cooldown_until") or 0.0),
            exhausted_until=float(raw.get("exhausted_until") or 0.0),
            last_error=_scrub(str(raw.get("last_error") or "")),
            requests=max(0, int(raw.get("requests") or 0)),
            successes=max(0, int(raw.get("successes") or 0)),
            failures=max(0, int(raw.get("failures") or 0)),
            prompt_tokens=max(0, int(raw.get("prompt_tokens") or 0)),
            completion_tokens=max(0, int(raw.get("completion_tokens") or 0)),
            latency_ms=max(0.0, float(raw.get("latency_ms") or 0.0)),
            speed_tps=max(0.0, float(raw.get("speed_tps") or 0.0)),
            added_at=str(raw.get("added_at") or ""),
            last_used_at=str(raw.get("last_used_at") or ""),
        )


class ProviderFailure(Exception):
    """Échec classé d'un fournisseur : la clé change d'état, l'appel continue."""

    def __init__(self, kind: str, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = _scrub(message)
        self.retry_after_s = retry_after_s


def _json_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            return decode_arguments(value)
        except Exception:  # noqa: BLE001 - un argument illisible devient vide
            return {}
    return {}


def to_openai_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Historique unifié vers le format OpenAI (arguments en chaîne JSON)."""
    prepared: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "tool":
            item.pop("name", None)
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            converted: list[dict[str, Any]] = []
            for index, call in enumerate(calls):
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                name = str(function.get("name") or call.get("name") or "").strip()
                if not name:
                    continue
                arguments = function.get("arguments", call.get("arguments", {}))
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                converted.append(
                    {
                        "id": str(call.get("id") or f"call_{index}"),
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                )
            if converted:
                item["tool_calls"] = converted
            else:
                item.pop("tool_calls", None)
        prepared.append(item)
    return prepared


def to_anthropic_messages(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Historique unifié vers l'API Messages d'Anthropic.

    Les appels d'outils deviennent des blocs `tool_use`, les résultats des blocs
    `tool_result` ; le prompt système est extrait séparément.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            text = str(content or "").strip()
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": str(content or ""),
            }
            if converted and converted[-1]["role"] == "user" and isinstance(
                converted[-1]["content"], list
            ):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = str(content or "").strip()
            if text:
                blocks.append({"type": "text", "text": text})
            for index, call in enumerate(message.get("tool_calls") or []):
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                name = str(function.get("name") or call.get("name") or "").strip()
                if not name:
                    continue
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or f"call_{index}"),
                        "name": name,
                        "input": _json_arguments(
                            function.get("arguments", call.get("arguments", {}))
                        ),
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        text = str(content or "")
        if converted and converted[-1]["role"] == "user":
            previous = converted[-1]["content"]
            if isinstance(previous, str):
                converted[-1]["content"] = f"{previous}\n{text}".strip()
            else:
                previous.append({"type": "text", "text": text})
        else:
            converted.append({"role": "user", "content": text})
    return "\n\n".join(system_parts), converted


def to_anthropic_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _ollama_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {
            "role": str(message.get("role") or "user"),
            "content": str(message.get("content") or ""),
        }
        if item["role"] == "tool":
            item["tool_name"] = str(message.get("name") or "tool")
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            tool_calls: list[dict[str, Any]] = []
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                name = str(function.get("name") or call.get("name") or "").strip()
                if not name:
                    continue
                tool_calls.append(
                    {
                        "function": {
                            "name": name,
                            "arguments": _json_arguments(
                                function.get("arguments", call.get("arguments", {}))
                            ),
                        }
                    }
                )
            if tool_calls:
                item["tool_calls"] = tool_calls
        prepared.append(item)
    return prepared


class LLMRouter:
    """Pool de clés chiffrées + classement best-first + bascule automatique."""

    supports_stream = False

    def __init__(
        self,
        settings: Settings,
        vault: Any,
        *,
        data_dir: str | Path | None = None,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = _now,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.data_dir = Path(data_dir) if data_dir else settings.paths.data_dir / "llm_router"
        self.state_path = self.data_dir / "keys.json"
        self._clock = clock
        self._lock = threading.RLock()
        self._abort = threading.Event()
        self._client = http_client
        self._owns_client = http_client is None
        self._keys: list[KeyRecord] = []
        self._observations: dict[str, dict[str, float]] = {}
        self._adjustments: dict[str, int] = {}
        self.current_model = ""
        self.current_provider = ""
        self.last_error = ""
        self._load()

    # -- persistance --------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        keys: list[KeyRecord] = []
        for item in raw.get("keys") or []:
            if isinstance(item, dict):
                record = KeyRecord.from_dict(item)
                if _KEY_ID.fullmatch(record.id):
                    keys.append(record)
        self._keys = keys[-5000:]
        observations = raw.get("observations")
        if isinstance(observations, dict):
            for model, data in observations.items():
                if isinstance(data, dict):
                    self._observations[str(model)] = {
                        key: float(data.get(key) or 0.0)
                        for key in ("requests", "successes", "failures", "latency_ms", "speed_tps")
                    }
        adjustments = raw.get("adjustments")
        if isinstance(adjustments, dict):
            self._adjustments = {
                str(model): max(-8, min(8, int(value)))
                for model, value in adjustments.items()
                if isinstance(value, (int, float))
            }
        self.current_model = str(raw.get("current_model") or "")
        provider = str(raw.get("current_provider") or "")
        self.current_provider = provider if provider in PROVIDERS else ""

    def _save(self) -> None:
        payload = {
            "version": 1,
            "keys": [record.to_dict() for record in self._keys],
            "observations": self._observations,
            "adjustments": self._adjustments,
            "current_model": self.current_model,
            "current_provider": self.current_provider,
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(self.state_path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            logger.warning("état du routeur non enregistré", exc_info=True)

    # -- pool de clés -------------------------------------------------------

    @staticmethod
    def _service(provider: str) -> str:
        return f"{_SERVICE_PREFIX}{provider}"

    @staticmethod
    def _hint(key: str) -> str:
        clean = str(key or "")
        if len(clean) <= 10:
            return "…"
        return f"{clean[:4]}…{clean[-4:]}"

    def add_key(self, key: str, *, provider: str = "auto", label: str = "") -> KeyRecord:
        """Enregistre une clé dans le coffre et ses métadonnées dans l'état.

        Le secret ne quitte jamais le coffre déverrouillé ; l'état ne garde
        qu'un indice masqué. Un fournisseur explicite est nécessaire pour les
        clés `sk-` ambiguës entre OpenAI et DeepSeek.
        """
        clean = str(key or "").strip()
        if not clean or any(char.isspace() for char in clean):
            raise ValueError("clé vide ou contenant un espace")
        if len(clean) < 12:
            raise ValueError("clé trop courte pour être une clé d'API")
        slug = str(provider or "auto").strip().lower()
        if slug in {"", "auto"}:
            slug = detect_provider(clean)
        if slug not in PROVIDERS or slug == "ollama":
            raise ValueError(f"fournisseur inconnu : {provider!r}")
        if not getattr(self.vault, "unlocked", False):
            raise RuntimeError("coffre verrouillé : déverrouillez-le pour ajouter une clé")
        key_id = secrets.token_hex(6)
        name = _scrub(
            (label or f"{PROVIDERS[slug].label} {len(self._keys) + 1}").strip()
        )[:80]
        self.vault.add(
            self._service(slug),
            key_id,
            clean,
            notes=name,
            tags=["llm", "api-key", slug],
        )
        record = KeyRecord(
            id=key_id,
            provider=slug,
            label=name,
            hint=self._hint(clean),
            added_at=_iso(),
        )
        with self._lock:
            self._keys.append(record)
            self._save()
        return record

    def remove_key(self, key_id: str) -> bool:
        identifier = str(key_id or "").strip()
        with self._lock:
            record = next((item for item in self._keys if item.id == identifier), None)
            if record is None:
                return False
            if not getattr(self.vault, "unlocked", False):
                raise RuntimeError(
                    "coffre verrouillé : déverrouillez-le pour supprimer une clé"
                )
            # Ne retire les métadonnées qu'une fois le secret supprimé : sinon
            # une clé resterait dans le coffre sans moyen de la retrouver.
            self.vault.delete(self._service(record.provider), record.id)
            self._keys = [item for item in self._keys if item.id != identifier]
            self._save()
        return True

    def reset_key(self, key_id: str) -> bool:
        """Remet une clé en service après un rechargement de crédits."""
        with self._lock:
            record = next((item for item in self._keys if item.id == str(key_id)), None)
            if record is None:
                return False
            record.status = ACTIVE
            record.cooldown_until = 0.0
            record.exhausted_until = 0.0
            record.last_error = ""
            self._save()
        return True

    def _secret(self, record: KeyRecord) -> str:
        if not getattr(self.vault, "unlocked", False):
            return ""
        try:
            credentials = self.vault.credentials(
                self._service(record.provider), record.id, count_use=False
            )
        except Exception:  # noqa: BLE001 - une clé illisible est indisponible
            return ""
        return str(getattr(credentials, "password", "") or "")

    def refresh(self) -> None:
        """Réactive les clés dont le minuteur est échu."""
        moment = self._clock()
        changed = False
        with self._lock:
            for record in self._keys:
                if record.status == COOLDOWN and record.cooldown_until <= moment:
                    record.status = ACTIVE
                    record.cooldown_until = 0.0
                    changed = True
                elif record.status == EXHAUSTED and record.exhausted_until <= moment:
                    record.status = ACTIVE
                    record.exhausted_until = 0.0
                    changed = True
            if changed:
                self._save()

    def _healthy_keys(self, provider: str) -> list[KeyRecord]:
        candidates = [
            record
            for record in self._keys
            if record.provider == provider and record.status == ACTIVE
        ]

        def health(record: KeyRecord) -> tuple[float, int]:
            attempts = record.successes + record.failures
            rate = record.successes / attempts if attempts else 1.0
            return (-rate, record.requests)

        return sorted(candidates, key=health)

    # -- classement ---------------------------------------------------------

    def _specs(self) -> list[ModelSpec]:
        return list(CATALOGUE)

    def _effective_score(self, spec: ModelSpec) -> int:
        return max(1, min(100, spec.score + int(self._adjustments.get(spec.model, 0))))

    def ranked_models(self) -> list[dict[str, Any]]:
        """Classement public, du plus puissant au moins puissant."""
        self.refresh()
        rows: list[dict[str, Any]] = []
        for spec in self._specs():
            observation = self._observations.get(spec.model, {})
            active = len(self._healthy_keys(spec.provider))
            rows.append(
                {
                    "provider": spec.provider,
                    "provider_label": PROVIDERS[spec.provider].label,
                    "model": spec.model,
                    "label": spec.label,
                    "base_score": spec.score,
                    "adjustment": int(self._adjustments.get(spec.model, 0)),
                    "score": self._effective_score(spec),
                    "specialties": list(spec.specialties),
                    "active_keys": active,
                    "requests": int(observation.get("requests", 0)),
                    "successes": int(observation.get("successes", 0)),
                    "failures": int(observation.get("failures", 0)),
                    "latency_ms": round(float(observation.get("latency_ms", 0.0)), 1),
                    "tokens_per_s": round(float(observation.get("speed_tps", 0.0)), 1),
                }
            )
        rows.sort(key=lambda row: (-int(row["score"]), row["model"]))
        return rows

    def _ordered_specs(self) -> list[ModelSpec]:
        rank = {row["model"]: index for index, row in enumerate(self.ranked_models())}
        return sorted(self._specs(), key=lambda spec: rank.get(spec.model, 999))

    def _ollama_spec(self) -> ModelSpec:
        return ModelSpec(
            "ollama",
            str(self.settings.llm_router.ollama_model or "llama3.2"),
            "Ollama (local)",
            80,
            ("fast_research",),
        )

    # -- états --------------------------------------------------------------

    def _record_success(
        self,
        spec: ModelSpec,
        record: KeyRecord | None,
        usage: Mapping[str, Any],
        latency_s: float,
    ) -> None:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
        speed = completion / latency_s if latency_s > 0 else 0.0
        with self._lock:
            observation = self._observations.setdefault(
                spec.model,
                {"requests": 0.0, "successes": 0.0, "failures": 0.0, "latency_ms": 0.0, "speed_tps": 0.0},
            )
            observation["requests"] += 1
            observation["successes"] += 1
            observation["latency_ms"] = round(
                observation["latency_ms"] * 0.7 + latency_s * 1000 * 0.3, 1
            )
            observation["speed_tps"] = round(
                observation["speed_tps"] * 0.7 + speed * 0.3, 2
            )
            if record is not None:
                record.requests += 1
                record.successes += 1
                record.prompt_tokens += prompt
                record.completion_tokens += completion
                record.latency_ms = round(record.latency_ms * 0.7 + latency_s * 1000 * 0.3, 1)
                record.speed_tps = round(record.speed_tps * 0.7 + speed * 0.3, 2)
                record.status = ACTIVE
                record.last_error = ""
                record.last_used_at = _iso()
            self.current_model = spec.model
            self.current_provider = spec.provider
            self.last_error = ""
            self._save()

    def _record_failure(
        self, spec: ModelSpec, record: KeyRecord | None, failure: ProviderFailure
    ) -> None:
        now = self._clock()
        with self._lock:
            observation = self._observations.setdefault(
                spec.model,
                {"requests": 0.0, "successes": 0.0, "failures": 0.0, "latency_ms": 0.0, "speed_tps": 0.0},
            )
            observation["requests"] += 1
            observation["failures"] += 1
            if record is not None:
                record.requests += 1
                record.failures += 1
                record.last_error = failure.message[:300]
                if failure.kind == "dead":
                    record.status = DEAD
                elif failure.kind == "exhausted":
                    record.status = EXHAUSTED
                    record.exhausted_until = now + self.settings.llm_router.exhausted_hours * 3600
                elif failure.kind == "cooldown":
                    record.status = COOLDOWN
                    delay = failure.retry_after_s
                    if delay is None or delay <= 0:
                        delay = self.settings.llm_router.cooldown_s
                    record.cooldown_until = now + max(5.0, float(delay))
            self.last_error = failure.message
            self._save()

    # -- HTTP et adaptateurs ------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.settings.llm.request_timeout_s,
                follow_redirects=False,
                headers={"User-Agent": f"{APP_ID}/1.0"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def abort(self) -> None:
        """Demande l'arrêt : effectif avant la tentative suivante."""
        self._abort.set()

    def available(self) -> bool:
        self.refresh()
        if str(self.settings.llm.ollama_base_url or "").strip():
            return True
        return any(record.status == ACTIVE for record in self._keys)

    def _post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, str, Mapping[str, str]]:
        try:
            response = self._http().post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderFailure("cooldown", f"délai dépassé : {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderFailure("cooldown", f"transport impossible : {exc}") from exc
        return response.status_code, response.text, response.headers

    @staticmethod
    def _quota_marker(body: str) -> bool:
        lowered = str(body or "").lower()
        return any(
            marker in lowered
            for marker in ("insufficient_quota", "insufficient quota", "credit balance", "billing")
        )

    def _raise_status(
        self, status: int, body: str, headers: Mapping[str, str]
    ) -> None:
        if status < 400:
            return
        snippet = " ".join(str(body or "").split())[:300]
        if status in {401, 403}:
            raise ProviderFailure("dead", f"clé refusée (HTTP {status}) : {snippet}")
        if status == 402 or (status == 429 and self._quota_marker(body)):
            raise ProviderFailure("exhausted", f"crédits épuisés (HTTP {status}) : {snippet}")
        if status == 429:
            delay: float | None = None
            try:
                raw_delay = str(headers.get("retry-after") or "").strip()
                delay = float(raw_delay) if raw_delay else None
            except ValueError:
                delay = None
            raise ProviderFailure(
                "cooldown", f"limite de débit (HTTP 429) : {snippet}", retry_after_s=delay
            )
        if status >= 500 or status in {404, 408, 409, 425}:
            raise ProviderFailure("cooldown", f"fournisseur indisponible (HTTP {status}) : {snippet}")
        raise ProviderFailure("fatal", f"requête refusée (HTTP {status}) : {snippet}")

    def _call(
        self,
        spec: ModelSpec,
        key: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        temperature: float | None,
    ) -> LLMResponse:
        provider = PROVIDERS[spec.provider]
        temperature_value = (
            self.settings.llm.temperature if temperature is None else float(temperature)
        )
        if provider.style == "ollama":
            base = str(self.settings.llm.ollama_base_url or "").rstrip("/")
            if not base:
                raise ProviderFailure("fatal", "ollama_base_url est vide")
            payload: dict[str, Any] = {
                "model": spec.model,
                "stream": False,
                "keep_alive": self.settings.llm.ollama_keep_alive,
                "messages": _ollama_messages(messages),
                "options": {
                    "temperature": temperature_value,
                    "top_p": self.settings.llm.top_p,
                    "num_predict": self.settings.llm.max_output_tokens,
                },
            }
            if tools:
                payload["tools"] = list(tools)
            status, body, headers = self._post(
                f"{base}/api/chat", {"Content-Type": "application/json"}, payload
            )
            self._raise_status(status, body, headers)
            return self._parse_ollama(body, spec)

        if provider.style == "anthropic":
            system, converted = to_anthropic_messages(messages)
            payload = {
                "model": spec.model,
                "max_tokens": self.settings.llm.max_output_tokens,
                "temperature": temperature_value,
                "messages": converted,
            }
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = to_anthropic_tools(tools)
            status, body, headers = self._post(
                f"{provider.base_url.rstrip('/')}/messages",
                {
                    "Content-Type": "application/json",
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                payload,
            )
            self._raise_status(status, body, headers)
            return self._parse_anthropic(body, spec)

        payload = {
            "model": spec.model,
            "messages": to_openai_messages(messages),
            "temperature": temperature_value,
            "top_p": self.settings.llm.top_p,
            "max_tokens": self.settings.llm.max_output_tokens,
            "stream": False,
        }
        if spec.provider == "openai" and (
            spec.model.startswith("gpt-5") or spec.model.startswith("o")
        ):
            # Les modèles OpenAI récents refusent `max_tokens` et les valeurs
            # de température non standard : on suit leur contrat natif.
            payload.pop("max_tokens", None)
            payload.pop("temperature", None)
            payload.pop("top_p", None)
            payload["max_completion_tokens"] = self.settings.llm.max_output_tokens
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        if spec.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/kontagafamakan98-rgb/OmniAgent"
            headers["X-Title"] = APP_ID
        status, body, response_headers = self._post(
            f"{provider.base_url.rstrip('/')}/chat/completions", headers, payload
        )
        self._raise_status(status, body, response_headers)
        return self._parse_openai(body, spec)

    def _decode(self, body: str) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise ProviderFailure("cooldown", "réponse du fournisseur non JSON") from exc
        if not isinstance(data, dict):
            raise ProviderFailure("cooldown", "réponse du fournisseur de type inattendu")
        return data

    @staticmethod
    def _usage(data: Mapping[str, Any], *, ollama: bool = False) -> dict[str, int]:
        raw = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        if ollama:
            prompt = int(data.get("prompt_eval_count") or 0)
            completion = int(data.get("eval_count") or 0)
        else:
            prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
            completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
        return {
            "prompt_tokens": max(0, prompt),
            "completion_tokens": max(0, completion),
            "total_tokens": max(0, prompt) + max(0, completion),
        }

    def _parse_openai(self, body: str, spec: ModelSpec) -> LLMResponse:
        data = self._decode(body)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderFailure("cooldown", "réponse sans 'choices' exploitable")
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(str(part.get("text", "")) for part in text if isinstance(part, Mapping))
        calls: list[ParsedToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            calls.append(
                ParsedToolCall(
                    name=name,
                    arguments=_json_arguments(function.get("arguments")),
                    raw=json.dumps(item, ensure_ascii=False),
                    source="router",
                    call_id=str(item.get("id") or ""),
                )
            )
        return LLMResponse(
            text=str(text).strip(),
            tool_calls=calls,
            model=str(data.get("model") or spec.model),
            finish_reason=str(first.get("finish_reason") or ""),
            usage=self._usage(data),
            raw=data,
        )

    def _parse_anthropic(self, body: str, spec: ModelSpec) -> LLMResponse:
        data = self._decode(body)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderFailure("cooldown", "réponse Anthropic sans contenu")
        texts: list[str] = []
        calls: list[ParsedToolCall] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "").strip()
                if not name:
                    continue
                calls.append(
                    ParsedToolCall(
                        name=name,
                        arguments=_json_arguments(block.get("input")),
                        raw=json.dumps(block, ensure_ascii=False),
                        source="router",
                        call_id=str(block.get("id") or ""),
                    )
                )
        return LLMResponse(
            text="".join(texts).strip(),
            tool_calls=calls,
            model=str(data.get("model") or spec.model),
            finish_reason=str(data.get("stop_reason") or ""),
            usage=self._usage(data),
            raw=data,
        )

    def _parse_ollama(self, body: str, spec: ModelSpec) -> LLMResponse:
        data = self._decode(body)
        message = data.get("message") if isinstance(data.get("message"), Mapping) else {}
        calls: list[ParsedToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            calls.append(
                ParsedToolCall(
                    name=name,
                    arguments=_json_arguments(function.get("arguments")),
                    raw=json.dumps(item, ensure_ascii=False),
                    source="router",
                )
            )
        return LLMResponse(
            text=str(message.get("content") or "").strip(),
            tool_calls=calls,
            model=str(data.get("model") or spec.model),
            finish_reason=str(data.get("done_reason") or ("stop" if data.get("done") else "")),
            usage=self._usage(data, ollama=True),
            raw=data,
        )

    # -- appel public -------------------------------------------------------

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        model: str = "",
        temperature: float | None = None,
    ) -> LLMResponse:
        """Meilleur modèle disponible, avec bascule clé → modèle → Ollama."""
        if not messages:
            raise LLMError("aucun message à envoyer", code="llm_empty")
        self._abort.clear()
        self.refresh()
        attempts = 0
        last_error: ProviderFailure | None = None
        forced = str(model or "").strip()

        for spec in self._ordered_specs():
            if forced and spec.model != forced:
                continue
            for record in self._healthy_keys(spec.provider):
                if attempts >= self.settings.llm_router.max_attempts:
                    break
                if self._abort.is_set():
                    raise LLMError(
                        "génération annulée par l'utilisateur",
                        code="llm_aborted",
                        retryable=False,
                    )
                secret = self._secret(record)
                if not secret:
                    continue
                attempts += 1
                started = time.perf_counter()
                try:
                    response = self._call(spec, secret, messages, tools, temperature)
                except ProviderFailure as failure:
                    last_error = failure
                    self._record_failure(spec, record, failure)
                    if failure.kind == "fatal":
                        raise LLMError(
                            failure.message, code="llm_http_error", retryable=False
                        ) from failure
                    continue
                latency = max(0.001, time.perf_counter() - started)
                self._record_success(spec, record, response.usage, latency)
                return response
            if attempts >= self.settings.llm_router.max_attempts:
                break

        # Filet de sécurité : le local prend le relais quand le cloud échoue.
        local = self._ollama_spec()
        if (not forced or forced == local.model) and attempts < self.settings.llm_router.max_attempts:
            attempts += 1
            started = time.perf_counter()
            try:
                response = self._call(local, "", messages, tools, temperature)
            except ProviderFailure as failure:
                last_error = failure
                self._record_failure(local, None, failure)
            else:
                latency = max(0.001, time.perf_counter() - started)
                self._record_success(local, None, response.usage, latency)
                return response

        detail = last_error.message if last_error is not None else "aucune clé active"
        raise LLMError(
            f"aucun fournisseur disponible : {detail}",
            code="llm_router_unavailable",
            retryable=False,
            details={"attempts": attempts},
        )

    def stream_complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        on_token: Callable[[str], None] | None = None,
        model: str = "",
        temperature: float | None = None,
    ) -> LLMResponse:
        """Compatibilité : réponse complète, remise en un fragment."""
        response = self.complete(
            messages, tools, model=model, temperature=temperature
        )
        if on_token is not None and response.text:
            on_token(response.text)
        return response

    def stream_text(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str = "",
        temperature: float | None = None,
    ):
        response = self.complete(messages, model=model, temperature=temperature)
        if response.text:
            yield response.text

    # -- optimisation et introspection -------------------------------------

    def optimize_scores(self) -> list[dict[str, Any]]:
        """Ajuste les scores à partir des mesures réelles, borné à ±8 points."""
        with self._lock:
            for spec in self._specs():
                observation = self._observations.get(spec.model)
                if not observation:
                    self._adjustments[spec.model] = 0
                    continue
                attempts = observation["successes"] + observation["failures"]
                rate = observation["successes"] / attempts if attempts else 1.0
                speed = observation["speed_tps"]
                speed_bonus = min(4.0, max(0.0, speed / 40.0))
                adjustment = round((rate - 0.5) * 10 + speed_bonus)
                self._adjustments[spec.model] = max(-8, min(8, int(adjustment)))
            self._save()
        return self.ranked_models()

    def summary(self) -> dict[str, Any]:
        """Vue compacte pour le diagnostic d'auto-évolution."""
        self.refresh()
        counts = {state: 0 for state in (ACTIVE, COOLDOWN, EXHAUSTED, DEAD)}
        for record in self._keys:
            counts[record.status] = counts.get(record.status, 0) + 1
        return {
            "current_model": self.current_model,
            "current_provider": self.current_provider,
            "keys": len(self._keys),
            "states": counts,
            "last_error": self.last_error,
        }

    def key_statuses(self) -> list[dict[str, Any]]:
        """État de chaque clé pour l'interface, sans jamais exposer le secret."""
        self.refresh()
        moment = self._clock()
        rows: list[dict[str, Any]] = []
        with self._lock:
            for record in self._keys:
                if record.status == COOLDOWN:
                    remaining = max(0, int(record.cooldown_until - moment))
                elif record.status == EXHAUSTED:
                    remaining = max(0, int(record.exhausted_until - moment))
                else:
                    remaining = 0
                rows.append(
                    {
                        "id": record.id,
                        "provider": record.provider,
                        "provider_label": PROVIDERS.get(record.provider, PROVIDERS["openai"]).label,
                        "label": record.label,
                        "hint": record.hint,
                        "status": record.status,
                        "remaining_s": remaining,
                        "requests": record.requests,
                        "successes": record.successes,
                        "failures": record.failures,
                        "tokens": record.total_tokens,
                        "latency_ms": record.latency_ms,
                        "tokens_per_s": record.speed_tps,
                        "last_error": record.last_error,
                    }
                )
        rows.sort(key=lambda row: (row["provider"], row["label"].lower()))
        return rows

    def format_status(self) -> str:
        """Rendu texte pour le panneau ou les journaux."""
        self.refresh()
        model = self.current_model or "aucun appel réussi"
        lines = [f"Modèle courant : {model}"]
        if self.current_provider:
            lines[-1] += f" ({PROVIDERS[self.current_provider].label})"
        active_ranks = [row for row in self.ranked_models() if row["active_keys"]]
        if active_ranks:
            best = active_ranks[0]
            lines.append(
                f"Meilleur disponible : {best['label']} (score {best['score']}, "
                f"{best['active_keys']} clé(s) active(s))"
            )
        else:
            lines.append("Aucune clé cloud active : Ollama local sert de secours.")
        for row in self.key_statuses():
            state = row["status"]
            if state == COOLDOWN and row["remaining_s"]:
                state += f" ({row['remaining_s']} s)"
            elif state == EXHAUSTED and row["remaining_s"]:
                state += f" ({row['remaining_s'] // 3600} h)"
            lines.append(
                f"{row['provider_label']} · {row['label']} · {row['hint']} · {state} · "
                f"{row['tokens']} tokens"
            )
        if len(lines) == 1:
            lines.append("Aucune clé enregistrée : Ollama local sert de secours.")
        return "\n".join(lines)
