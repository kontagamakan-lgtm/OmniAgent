"""Parseur tolérant des appels d'outils produits par un modèle.

Les petits modèles n'obéissent pas toujours au format natif : certains
écrivent un bloc `<tool_call>`, d'autres un bloc Markdown ```json```, d'autres
encore un objet JSON au milieu d'une phrase. Ce module récupère l'appel dans
tous ces cas, sans jamais exécuter de code : seul `json.loads` (puis
`ast.literal_eval` en repli) est utilisé.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from core.errors import ToolCallError

__all__ = [
    "ParsedToolCall",
    "decode_arguments",
    "extract_json_objects",
    "final_text",
    "has_final_answer",
    "parse_tool_calls",
    "strip_final_prefix",
]

_ALLOWED_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")
_TOOL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|tool_call|tool)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_FINAL_PREFIXES = (
    "réponse finale :",
    "reponse finale :",
    "final answer:",
    "final answer :",
    "réponse :",
)
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARGS_KEYS = ("arguments", "args", "parameters", "input", "params")


@dataclass(slots=True)
class ParsedToolCall:
    """Un appel d'outil extrait d'une sortie de modèle."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    source: str = "json"
    call_id: str = ""

    def to_openai(self) -> dict[str, Any]:
        """Forme attendue dans `messages[*].tool_calls`."""
        return {
            "id": self.call_id or f"call_{self.name}",
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    def signature(self) -> tuple[str, str]:
        """Empreinte stable pour la détection de boucles."""
        return self.name, json.dumps(self.arguments, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Extraction JSON tolérante
# ---------------------------------------------------------------------------


def extract_json_objects(text: str) -> Iterator[str]:
    """Itère sur les objets `{…}` équilibrés d'un texte, chaînes respectées."""
    body = str(text or "")
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield body[start : index + 1]
                    start = -1


def _loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        value = ast.literal_eval(candidate)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, (dict, list)) else None


def decode_arguments(value: Any) -> dict[str, Any]:
    """Normalise les arguments : objet, chaîne JSON, ou littéral Python."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = _loads(value.strip())
        if isinstance(decoded, dict):
            return decoded
        raise ToolCallError(
            "arguments d'outil illisibles (JSON attendu)",
            details={"arguments": value[:200]},
        )
    raise ToolCallError(
        f"arguments d'outil de type inattendu : {type(value).__name__}",
        details={"arguments": repr(value)[:200]},
    )


# ---------------------------------------------------------------------------
# Extraction des appels
# ---------------------------------------------------------------------------


def _candidate_calls(payload: Any, *, raw: str, source: str) -> list[ParsedToolCall]:
    items = payload if isinstance(payload, list) else [payload]
    calls: list[ParsedToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Forme « function calling » imbriquée : {"function": {"name": …}}
        body = item
        nested = item.get("function")
        if isinstance(nested, dict):
            body = {**nested, **{k: v for k, v in item.items() if k != "function"}}
        name = ""
        for key in _NAME_KEYS:
            candidate = body.get(key)
            if isinstance(candidate, str) and candidate.strip():
                name = candidate.strip()
                break
        if not name or not _ALLOWED_NAME.match(name):
            continue
        arguments: Any = None
        for key in _ARGS_KEYS:
            if key in body:
                arguments = body[key]
                break
        try:
            decoded = decode_arguments(arguments)
        except ToolCallError:
            continue
        call_id = str(item.get("id") or body.get("id") or "")
        calls.append(
            ParsedToolCall(
                name=name,
                arguments=decoded,
                raw=raw,
                source=source,
                call_id=call_id,
            )
        )
    return calls


def parse_tool_calls(text: str) -> list[ParsedToolCall]:
    """Extrait tous les appels d'outils d'une sortie de modèle.

    Ordre de recherche : balises `<tool_call>`, blocs de code, JSON intégral,
    puis objets JSON noyés dans la prose. Les doublons stricts sont éliminés.
    """
    body = str(text or "")
    if not body.strip():
        return []

    found: list[ParsedToolCall] = []
    for match in _TOOL_TAG.finditer(body):
        payload = _loads(match.group(1).strip())
        found.extend(_candidate_calls(payload, raw=match.group(0), source="tag"))

    if not found:
        for match in _FENCE.finditer(body):
            payload = _loads(match.group(1).strip())
            found.extend(_candidate_calls(payload, raw=match.group(0), source="fence"))

    if not found:
        stripped = body.strip()
        payload = _loads(stripped) if stripped[:1] in "{[" else None
        if payload is not None:
            found.extend(_candidate_calls(payload, raw=stripped, source="json"))

    if not found:
        for candidate in extract_json_objects(body):
            payload = _loads(candidate)
            found.extend(_candidate_calls(payload, raw=candidate, source="inline"))

    unique: list[ParsedToolCall] = []
    seen: set[tuple[str, str]] = set()
    for call in found:
        signature = call.signature()
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(call)
    return unique


def has_final_answer(text: str) -> bool:
    """Vrai si le texte ressemble à une conclusion sans appel d'outil."""
    body = str(text or "").strip()
    if not body:
        return False
    if parse_tool_calls(body):
        return False
    lowered = body.lower()
    return not lowered.startswith(("{", "[")) and len(body) >= 2


def strip_final_prefix(text: str) -> str:
    """Retire un éventuel préfixe « Réponse finale : » ou équivalent."""
    body = str(text or "").strip()
    lowered = body.lower()
    for prefix in _FINAL_PREFIXES:
        if lowered.startswith(prefix):
            return body[len(prefix) :].strip()
    return body


def final_text(text: str) -> str:
    """Texte final nettoyé : préfixe retiré, blocs d'appel supprimés."""
    body = _TOOL_TAG.sub("", str(text or ""))
    body = strip_final_prefix(body)
    return body.strip()
