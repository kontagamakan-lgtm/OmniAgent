"""Client de génération : endpoint compatible OpenAI, ou Ollama local.

Deux méthodes seulement sont exposées, et elles suffisent à la boucle ReAct :

* `complete()` — une réponse complète, avec appels d'outils éventuels ;
* `stream_complete()` — le même contrat en flux : le texte est remis fragment
  par fragment, les appels d'outils sont accumulés puis rendus en une fois ;
* `stream_text()` — variante texte seul, sans appels d'outils.

Les erreurs sont converties dans la hiérarchie de `core.errors` : la boucle
sait ainsi s'il faut réessayer, attendre, ou s'arrêter immédiatement. Aucun
message d'erreur ne contient la clé d'API.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

import httpx

from config import APP_ID, LLMConfig
from core.errors import (
    ConfigError,
    LLMAuthError,
    LLMError,
    LLMRateLimited,
    LLMResponseError,
    retry_delay,
)
from core.react_parser import ParsedToolCall, decode_arguments

__all__ = ["LLMClient", "LLMResponse"]

logger = logging.getLogger(f"{APP_ID}.llm")

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _aborted_error() -> LLMError:
    """Erreur d'un flux fermé volontairement (jamais une panne à réessayer)."""
    return LLMError(
        "génération annulée par l'utilisateur",
        code="llm_aborted",
        retryable=False,
    )


@dataclass(slots=True)
class LLMResponse:
    """Réponse normalisée, quel que soit le fournisseur."""

    text: str = ""
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> dict[str, Any]:
        """Message assistant réinjectable dans l'historique."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = [call.to_openai() for call in self.tool_calls]
        return message


class LLMClient:
    """Appels HTTP vers DeepSeek/OpenAI-compatible ou Ollama."""

    #: `AgentCore` s'en sert pour choisir `stream_complete` quand c'est possible.
    supports_stream = True

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        self.request_count = 0
        self.retry_count = 0
        self._abort = threading.Event()
        self._active_lock = threading.Lock()
        self._active: httpx.Response | None = None

    # -- cycle de vie -------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.cfg.request_timeout_s,
                follow_redirects=False,
                headers={"User-Agent": f"{APP_ID}/1.0"},
            )
        return self._client

    def abort(self) -> None:
        """Ferme le flux en cours : plus d'attente jusqu'au prochain fragment.

        Appelable depuis un autre thread que celui qui lit la réponse — c'est le
        chemin emprunté par le bouton « Annuler ». Sans flux actif, sans effet.
        """
        self._abort.set()
        with self._active_lock:
            response = self._active
        if response is None:
            return
        try:
            response.close()
        except Exception:  # noqa: BLE001 - un flux déjà refermé n'est pas une erreur
            logger.debug("flux déjà refermé au moment de l'annulation", exc_info=True)

    def _mark_active(self, response: httpx.Response | None) -> None:
        with self._active_lock:
            self._active = response

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- disponibilité ------------------------------------------------------

    def available(self) -> bool:
        if self.cfg.backend == "ollama":
            return bool(self.cfg.ollama_base_url.strip())
        return bool(self.cfg.api_key())

    def _require_credentials(self) -> None:
        if self.cfg.backend == "ollama":
            if not self.cfg.ollama_base_url.strip():
                raise ConfigError("ollama_base_url est vide", code="missing_ollama_url")
            return
        if not self.cfg.api_key():
            raise LLMAuthError(
                f"clé d'API absente : renseignez la variable {self.cfg.api_key_env}",
                code="llm_missing_key",
            )

    # -- requêtes -----------------------------------------------------------

    def _endpoint(self, model: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """URL, en-têtes et charge utile communs aux deux modes."""
        if self.cfg.backend == "ollama":
            url = f"{self.cfg.ollama_base_url.rstrip('/')}/api/chat"
            payload: dict[str, Any] = {
                "model": model,
                "stream": False,
                "keep_alive": self.cfg.ollama_keep_alive,
                "options": {
                    "temperature": self.cfg.temperature,
                    "top_p": self.cfg.top_p,
                    "num_predict": self.cfg.max_output_tokens,
                },
            }
            return url, {"Content-Type": "application/json"}, payload
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key()}",
        }
        return url, headers, {}

    def _raise_for_status(self, response: httpx.Response, body: str) -> None:
        status = response.status_code
        if status < 400:
            return
        snippet = body.strip().replace("\n", " ")[:300]
        if status in {401, 403}:
            raise LLMAuthError(
                f"accès refusé par le fournisseur (HTTP {status}) : vérifiez la clé "
                f"{self.cfg.api_key_env}",
                code="llm_auth",
                details={"status": status},
            )
        if status == 429:
            retry_after = response.headers.get("retry-after", "")
            delay = None
            try:
                delay = float(retry_after) if retry_after else None
            except ValueError:
                delay = None
            raise LLMRateLimited(
                f"quota atteint (HTTP 429) : {snippet}",
                retry_after_s=delay,
                details={"status": status},
            )
        if status in _RETRYABLE_STATUS:
            raise LLMError(
                f"fournisseur indisponible (HTTP {status}) : {snippet}",
                code="llm_unavailable",
                details={"status": status},
            )
        raise LLMError(
            f"requête refusée (HTTP {status}) : {snippet}",
            code="llm_http_error",
            retryable=False,
            details={"status": status},
        )

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http().post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(f"délai dépassé en contactant le fournisseur : {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"transport impossible vers le fournisseur : {exc}") from exc
        self.request_count += 1
        body = response.text
        self._raise_for_status(response, body)
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMResponseError(
                "réponse du fournisseur non JSON", details={"body": body[:300]}
            ) from exc
        if not isinstance(data, dict):
            raise LLMResponseError("réponse du fournisseur de type inattendu")
        return data

    # -- normalisation ------------------------------------------------------

    def _messages_for_provider(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Adapte l'historique au protocole du fournisseur.

        Un message `tool` porte `tool_call_id` et `content` ; le nom de l'outil
        n'est attendu qu'en mode Ollama (`tool_name`) et peut faire rejeter la
        requête par un endpoint OpenAI strict.
        """
        prepared: list[dict[str, Any]] = []
        for message in messages:
            item = dict(message)
            if item.get("role") == "tool":
                name = str(item.pop("name", "") or "")
                if self.cfg.backend == "ollama" and name:
                    item["tool_name"] = name
            prepared.append(item)
        return prepared

    def _parse_openai(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("réponse sans 'choices' exploitable", details=data)
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = message.get("content") or ""
        if isinstance(text, list):  # certains fournisseurs renvoient des segments
            text = "".join(
                str(part.get("text", "")) for part in text if isinstance(part, dict)
            )
        calls: list[ParsedToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            try:
                arguments = decode_arguments(function.get("arguments"))
            except Exception:  # noqa: BLE001 - un appel illisible est ignoré, pas fatal
                arguments = {}
            calls.append(
                ParsedToolCall(
                    name=name,
                    arguments=arguments,
                    raw=json.dumps(item, ensure_ascii=False),
                    source="provider",
                    call_id=str(item.get("id") or ""),
                )
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return LLMResponse(
            text=str(text).strip(),
            tool_calls=calls,
            model=str(data.get("model") or self.cfg.model),
            finish_reason=str(first.get("finish_reason") or ""),
            usage=dict(usage),
            raw=data,
        )

    def _parse_ollama(self, data: dict[str, Any]) -> LLMResponse:
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        text = str(message.get("content") or "").strip()
        calls: list[ParsedToolCall] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            try:
                arguments = decode_arguments(function.get("arguments"))
            except Exception:  # noqa: BLE001
                arguments = {}
            calls.append(
                ParsedToolCall(
                    name=name,
                    arguments=arguments,
                    raw=json.dumps(item, ensure_ascii=False),
                    source="provider",
                )
            )
        usage = {
            key: data[key]
            for key in ("eval_count", "prompt_eval_count", "total_duration")
            if key in data
        }
        return LLMResponse(
            text=text,
            tool_calls=calls,
            model=str(data.get("model") or self.cfg.model),
            finish_reason=str(data.get("done_reason") or ("stop" if data.get("done") else "")),
            usage=usage,
            raw=data,
        )

    # -- API publique -------------------------------------------------------

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        model: str = "",
        temperature: float | None = None,
    ) -> LLMResponse:
        """Un tour de génération, avec reprises sur erreurs transitoires."""
        self._require_credentials()
        chosen = model or self.cfg.model
        url, headers, extra = self._endpoint(chosen)
        payload: dict[str, Any] = {
            **extra,
            "messages": self._messages_for_provider(messages),
        }
        if self.cfg.backend == "ollama":
            payload["model"] = chosen
            payload["stream"] = False
        else:
            payload["model"] = chosen
            payload["temperature"] = (
                self.cfg.temperature if temperature is None else float(temperature)
            )
            payload["top_p"] = self.cfg.top_p
            payload["max_tokens"] = self.cfg.max_output_tokens
            payload["stream"] = False
        if tools:
            payload["tools"] = list(tools)
            if self.cfg.backend != "ollama":
                payload["tool_choice"] = "auto"

        attempts = max(0, int(self.cfg.max_retries)) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                data = self._post(url, headers, payload)
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt >= attempts:
                    raise
                delay = retry_delay(exc, attempt, self.cfg.retry_backoff_s)
                self.retry_count += 1
                logger.warning(
                    "reprise LLM %s/%s dans %.1f s (%s)",
                    attempt,
                    attempts - 1,
                    delay,
                    exc.code,
                )
                if delay:
                    self._sleep(delay)
                continue
            return (
                self._parse_ollama(data)
                if self.cfg.backend == "ollama"
                else self._parse_openai(data)
            )
        raise last_error or LLMError("échec inconnu du fournisseur")

    def stream_complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        on_token: Callable[[str], None] | None = None,
        model: str = "",
        temperature: float | None = None,
    ) -> LLMResponse:
        """Comme `complete`, mais chaque fragment de texte est remis en flux.

        Contrairement à `stream_text`, les appels d'outils sont accumulés : un
        tour qui décide d'agir passe par le même chemin et le texte affiché
        correspond à la réponse retenue. Aucune reprise interne : après une
        coupure, réémettre la demande dupliquerait le texte déjà montré — les
        reprises restent pilotées par `AgentCore`, qui sait où en est la boucle.
        """
        self._require_credentials()
        chosen = model or self.cfg.model
        url, headers, extra = self._endpoint(chosen)
        payload: dict[str, Any] = {
            **extra,
            "messages": self._messages_for_provider(messages),
        }
        if self.cfg.backend == "ollama":
            payload.update({"model": chosen, "stream": True})
        else:
            payload.update(
                {
                    "model": chosen,
                    "temperature": (
                        self.cfg.temperature if temperature is None else float(temperature)
                    ),
                    "top_p": self.cfg.top_p,
                    "max_tokens": self.cfg.max_output_tokens,
                    "stream": True,
                }
            )
        if tools:
            payload["tools"] = list(tools)
            if self.cfg.backend != "ollama":
                payload["tool_choice"] = "auto"

        parts: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        finish = ""
        usage: dict[str, Any] = {}
        # Une annulation antérieure ne doit pas tuer cette génération-ci.
        self._abort.clear()
        try:
            with self._http().stream("POST", url, json=payload, headers=headers) as response:
                self.request_count += 1
                if response.status_code >= 400:
                    self._raise_for_status(
                        response, response.read().decode("utf-8", "replace")
                    )
                self._mark_active(response)
                for line in response.iter_lines():
                    if self._abort.is_set():
                        raise _aborted_error()
                    text, calls, reason, chunk_usage = self._stream_event(line)
                    if text:
                        parts.append(text)
                        if on_token is not None:
                            on_token(text)
                    for call in calls:
                        slot = pending.setdefault(
                            int(call["index"]), {"id": "", "name": "", "arguments": ""}
                        )
                        slot["id"] = slot["id"] or str(call.get("id") or "")
                        slot["name"] = slot["name"] or str(call.get("name") or "")
                        arguments = call.get("arguments")
                        if isinstance(arguments, dict):
                            slot["arguments"] = json.dumps(arguments, ensure_ascii=False)
                        elif arguments:
                            slot["arguments"] += str(arguments)
                    if reason:
                        finish = reason
                    usage.update(chunk_usage or {})
        except httpx.TimeoutException as exc:
            if self._abort.is_set():
                raise _aborted_error() from exc
            raise LLMError(f"délai dépassé pendant le flux : {exc}") from exc
        except (httpx.HTTPError, httpx.StreamClosed) as exc:
            # Fermer la réponse sous les pieds du lecteur remonte ici : c'est
            # l'annulation demandée, pas une panne de transport.
            if self._abort.is_set():
                raise _aborted_error() from exc
            raise LLMError(f"flux interrompu : {exc}") from exc
        finally:
            self._mark_active(None)

        # Flux clos sans exception mais tronqué : ne jamais rendre un texte
        # partiel comme s'il était complet.
        if self._abort.is_set():
            raise _aborted_error()

        parsed_calls: list[ParsedToolCall] = []
        for index in sorted(pending):
            item = pending[index]
            name = str(item["name"] or "").strip()
            if not name:
                continue
            try:
                arguments = decode_arguments(item["arguments"])
            except Exception:  # noqa: BLE001 - un appel illisible est ignoré, pas fatal
                arguments = {}
            parsed_calls.append(
                ParsedToolCall(
                    name=name,
                    arguments=arguments,
                    raw=item["arguments"],
                    source="provider",
                    call_id=str(item["id"] or ""),
                )
            )

        return LLMResponse(
            text="".join(parts).strip(),
            tool_calls=parsed_calls,
            model=chosen,
            finish_reason=finish or ("tool_calls" if parsed_calls else "stop"),
            usage=usage,
        )

    def _stream_event(
        self, line: str
    ) -> tuple[str, list[dict[str, Any]], str, dict[str, Any]]:
        """Décode une ligne de flux : (texte, appels, finish_reason, usage)."""
        raw = (line or "").strip()
        if not raw:
            return "", [], "", {}

        if self.cfg.backend == "ollama":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return "", [], "", {}
            if not isinstance(data, dict):
                return "", [], "", {}
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            calls = [
                {
                    "index": index,
                    "id": str(item.get("id") or ""),
                    "name": str(
                        (item.get("function") or {}).get("name") or ""
                    ),
                    "arguments": (item.get("function") or {}).get("arguments") or {},
                }
                for index, item in enumerate(message.get("tool_calls") or [])
                if isinstance(item, dict)
            ]
            usage = {
                key: data[key]
                for key in ("eval_count", "prompt_eval_count", "total_duration")
                if key in data
            }
            reason = str(data.get("done_reason") or ("stop" if data.get("done") else ""))
            return str(message.get("content") or ""), calls, reason, usage

        if not raw.startswith("data:"):
            return "", [], "", {}
        payload = raw[len("data:") :].strip()
        if payload in {"", "[DONE]"}:
            return "", [], "", {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return "", [], "", {}
        if not isinstance(data, dict):
            return "", [], "", {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", [], "", dict(usage)
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        text = delta.get("content") or ""
        if isinstance(text, list):
            text = "".join(
                str(part.get("text", "")) for part in text if isinstance(part, dict)
            )
        calls: list[dict[str, Any]] = []
        for position, item in enumerate(delta.get("tool_calls") or []):
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            calls.append(
                {
                    "index": int(item.get("index", position) or position),
                    "id": str(item.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": function.get("arguments") or "",
                }
            )
        return (
            str(text),
            calls,
            str(first.get("finish_reason") or ""),
            dict(usage),
        )

    def stream_text(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str = "",
        temperature: float | None = None,
    ) -> Iterator[str]:
        """Génère le texte par fragments (sans appels d'outils)."""
        self._require_credentials()
        chosen = model or self.cfg.model
        url, headers, extra = self._endpoint(chosen)
        payload: dict[str, Any] = {**extra, "messages": self._messages_for_provider(messages)}
        if self.cfg.backend == "ollama":
            payload.update({"model": chosen, "stream": True})
        else:
            payload.update(
                {
                    "model": chosen,
                    "temperature": self.cfg.temperature if temperature is None else float(temperature),
                    "top_p": self.cfg.top_p,
                    "max_tokens": self.cfg.max_output_tokens,
                    "stream": True,
                }
            )

        try:
            with self._http().stream("POST", url, json=payload, headers=headers) as response:
                self.request_count += 1
                if response.status_code >= 400:
                    self._raise_for_status(response, response.read().decode("utf-8", "replace"))
                for line in response.iter_lines():
                    chunk = self._stream_chunk(line)
                    if chunk:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise LLMError(f"délai dépassé pendant le flux : {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"flux interrompu : {exc}") from exc

    def _stream_chunk(self, line: str) -> str:
        """Décode une ligne SSE (OpenAI) ou un objet JSON par ligne (Ollama)."""
        raw = (line or "").strip()
        if not raw:
            return ""
        if self.cfg.backend == "ollama":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return ""
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            return str(message.get("content") or "")
        if not raw.startswith("data:"):
            return ""
        payload = raw[len("data:") :].strip()
        if payload in {"", "[DONE]"}:
            return ""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else {}
        if not isinstance(delta, dict):
            return ""
        return str(delta.get("content") or "")
