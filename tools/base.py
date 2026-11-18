"""Socle commun des outils exposés au moteur ReAct.

Un `Tool` décrit sa signature en JSON Schema, valide les arguments reçus du
modèle, exécute l'action et renvoie toujours un `ToolResult` — jamais une
exception non gérée : la boucle de l'agent doit pouvoir analyser un échec et
changer de stratégie sans s'interrompre.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from config import APP_ID

__all__ = [
    "FunctionTool",
    "Tool",
    "ToolArgumentError",
    "ToolError",
    "ToolResult",
    "ToolSet",
    "object_schema",
    "param",
    "validate_arguments",
    "SENSITIVE_NOTICE",
]

SENSITIVE_NOTICE = (
    "[SENSIBLE] résultat non transmis au modèle : secret, identifiant ou cookie. "
    "Consultable uniquement en local, via le code Python ou l'interface."
)

logger = logging.getLogger(f"{APP_ID}.tools")

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": (list, tuple),
    "object": dict,
    "null": type(None),
}


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Échec d'exécution d'un outil, attendu et exploitable par l'agent."""

    code = "tool_error"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.details = details


class ToolArgumentError(ToolError):
    """Arguments non conformes au schéma déclaré."""

    code = "invalid_arguments"


# ---------------------------------------------------------------------------
# Résultat
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolResult:
    """Résultat normalisé d'un appel d'outil."""

    ok: bool = True
    data: Any = None
    error: str = ""
    error_code: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    sensitive: bool = False
    # Résumé transmissible au modèle quand `sensitive` est vrai (jamais de secret) :
    # permet à l'agent de garder du contexte sans voir les valeurs.
    safe_summary: str = ""

    def to_dict(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        """Vue sérialisable. Un résultat sensible est masqué par défaut.

        `include_sensitive=True` est réservé à l'affichage local (interface,
        journal) ; tout ce qui part vers un fournisseur LLM doit utiliser
        `to_text()` ou l'appel par défaut.
        """
        payload: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            if self.sensitive and not include_sensitive:
                payload["data"] = self.safe_summary or SENSITIVE_NOTICE
                payload["sensitive"] = True
            else:
                payload["data"] = self.data
        else:
            payload["error"] = self.error
            payload["error_code"] = self.error_code
        if self.meta:
            payload["meta"] = self.meta
        return payload

    def to_text(self, max_chars: int = 12000) -> str:
        """Représentation textuelle compacte pour le contexte du modèle.

        Un résultat marqué `sensitive` n'est **jamais** sérialisé ici : c'est la
        barrière qui empêche un cookie de session ou un mot de passe révélé de
        partir vers le fournisseur LLM. `data` reste accessible au code Python.
        """
        if self.sensitive and self.ok:
            return self.safe_summary or SENSITIVE_NOTICE
        if self.ok:
            body = self.data if isinstance(self.data, str) else json.dumps(
                self.data, ensure_ascii=False, default=str
            )
        else:
            body = f"ERREUR [{self.error_code}] {self.error}"
        if max_chars and len(body) > max_chars:
            body = body[:max_chars] + f"\n…[tronqué : {len(body) - max_chars} caractères]"
        return body

    @classmethod
    def failure(
        cls, error: str, *, code: str = "tool_error", duration_ms: float = 0.0
    ) -> "ToolResult":
        return cls(ok=False, error=error, error_code=code, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------


def param(
    type_: str,
    description: str,
    *,
    enum: Sequence[Any] | None = None,
    default: Any = None,
    minimum: float | None = None,
    maximum: float | None = None,
    min_items: int | None = None,
    max_items: int | None = None,
    items: dict[str, Any] | None = None,
    pattern: str | None = None,
) -> dict[str, Any]:
    """Construit la description d'un paramètre JSON Schema."""
    spec: dict[str, Any] = {"type": type_, "description": description}
    if enum is not None:
        spec["enum"] = list(enum)
    if default is not None:
        spec["default"] = default
    if minimum is not None:
        spec["minimum"] = minimum
    if maximum is not None:
        spec["maximum"] = maximum
    if min_items is not None:
        spec["minItems"] = min_items
    if max_items is not None:
        spec["maxItems"] = max_items
    if items is not None:
        spec["items"] = items
    if pattern is not None:
        spec["pattern"] = pattern
    return spec


def object_schema(
    properties: Mapping[str, dict[str, Any]],
    required: Iterable[str] = (),
    *,
    description: str = "",
) -> dict[str, Any]:
    """Schéma d'objet strict (`additionalProperties: false`)."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "required": [name for name in required],
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


def _coerce_scalar(value: Any, expected: str) -> Any:
    """Tolère les conversions sans ambiguïté fréquentes chez les modèles."""
    if expected == "integer" and isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    if expected == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    if expected == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "vrai", "1", "yes", "oui"}:
            return True
        if lowered in {"false", "faux", "0", "no", "non"}:
            return False
    return value


def _check_type(value: Any, expected: str, path: str) -> Any:
    if expected == "boolean":
        if not isinstance(value, bool):
            raise ToolArgumentError(f"'{path}' : booléen attendu, reçu {type(value).__name__}")
        return value
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolArgumentError(f"'{path}' : entier attendu, reçu {type(value).__name__}")
        return value
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolArgumentError(f"'{path}' : nombre attendu, reçu {type(value).__name__}")
        return value
    expected_type = _JSON_TYPES.get(expected)
    if expected_type is None:
        return value
    if not isinstance(value, expected_type):
        raise ToolArgumentError(
            f"'{path}' : {expected} attendu, reçu {type(value).__name__}"
        )
    return value


def _validate_node(schema: dict[str, Any], value: Any, path: str, depth: int = 0) -> Any:
    if depth > 12:
        raise ToolArgumentError("schéma d'arguments trop profond")
    if not isinstance(schema, dict):
        return value

    expected = schema.get("type")
    if isinstance(expected, list):  # union de types
        last_error: Exception | None = None
        for candidate in expected:
            try:
                return _validate_node({**schema, "type": candidate}, value, path, depth + 1)
            except ToolArgumentError as exc:
                last_error = exc
        raise last_error or ToolArgumentError(f"'{path}' : type invalide")

    if expected:
        value = _coerce_scalar(value, expected)
        value = _check_type(value, expected, path)

    if "enum" in schema and value not in schema["enum"]:
        raise ToolArgumentError(f"'{path}' : valeur {value!r} hors {schema['enum']}")
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            raise ToolArgumentError(f"'{path}' : minimum {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > schema["maximum"]:
            raise ToolArgumentError(f"'{path}' : maximum {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolArgumentError(f"'{path}' : longueur minimale {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolArgumentError(f"'{path}' : longueur maximale {schema['maxLength']}")
    if expected == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolArgumentError(f"'{path}' : au moins {schema['minItems']} élément(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolArgumentError(f"'{path}' : au plus {schema['maxItems']} élément(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [
                _validate_node(item_schema, item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(value)
            ]
    if expected == "object" and isinstance(value, dict):
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ToolArgumentError(
                    f"'{path}' : paramètres inconnus {sorted(unknown)}"
                )
        required = schema.get("required") or []
        missing = [name for name in required if name not in value]
        if missing:
            raise ToolArgumentError(f"'{path}' : paramètres requis manquants {missing}")
        return {
            key: _validate_node(properties[key], item, f"{path}.{key}", depth + 1)
            if key in properties
            else item
            for key, item in value.items()
        }
    return value


def validate_arguments(schema: dict[str, Any], arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Valide les arguments, applique les valeurs par défaut et retourne une copie."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ToolArgumentError("les arguments doivent former un objet JSON")
    payload = dict(arguments)

    properties: dict[str, Any] = schema.get("properties") or {}
    for name, spec in properties.items():
        if name not in payload and "default" in spec:
            payload[name] = spec["default"]

    return _validate_node(schema, payload, "arguments")


# ---------------------------------------------------------------------------
# Outil
# ---------------------------------------------------------------------------


class Tool:
    """Classe de base d'un outil exécutable par l'agent."""

    name: str = ""
    description: str = ""
    # Remplacé par chaque sous-classe dans son `__init__` ; jamais muté en place.
    parameters: dict[str, Any] = {}
    read_only: bool = True
    dangerous: bool = False
    sensitive: bool = False

    def schema(self) -> dict[str, Any]:
        """Spécification au format « function calling » d'OpenAI."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **arguments: Any) -> ToolResult:
        """Point d'entrée unique : validation, exécution, chronométrage."""
        started = time.perf_counter()
        try:
            validated = validate_arguments(self.parameters, arguments)
            data = self._run(**validated)
        except ToolArgumentError as exc:
            return ToolResult.failure(str(exc), code=exc.code, duration_ms=_elapsed(started))
        except ToolError as exc:
            return ToolResult.failure(str(exc), code=exc.code, duration_ms=_elapsed(started))
        except Exception as exc:  # noqa: BLE001 - l'agent doit voir l'échec, pas le crash
            logger.exception("Outil %s en échec inattendu", self.name)
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}", code="internal_error", duration_ms=_elapsed(started)
            )

        if isinstance(data, ToolResult):
            data.duration_ms = _elapsed(started)
            return data
        return ToolResult(ok=True, data=data, duration_ms=_elapsed(started), sensitive=self.sensitive)

    def _run(self, **arguments: Any) -> Any:  # pragma: no cover - abstrait
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - confort
        return f"<{type(self).__name__} {self.name}>"


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class FunctionTool(Tool):
    """Adapte une méthode Python en outil, sans classe dédiée par action."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable[..., Any],
        *,
        read_only: bool = True,
        dangerous: bool = False,
        sensitive: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func = func
        self.read_only = read_only
        self.dangerous = dangerous
        self.sensitive = sensitive

    def _run(self, **arguments: Any) -> Any:
        return self._func(**arguments)


class ToolSet:
    """Collection nommée d'outils, prête pour le registre de l'agent."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("un outil doit déclarer un nom")
        if tool.name in self._tools:
            raise ValueError(f"outil déjà enregistré: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools()]

    def dispatch(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(
                f"outil inconnu '{name}' ; disponibles : {', '.join(self.names())}",
                code="unknown_tool",
            )
        return tool.execute(**dict(arguments or {}))

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
