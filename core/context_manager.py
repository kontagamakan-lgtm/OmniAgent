"""Budget de contexte : repli de l'historique de la boucle ReAct.

Le fournisseur refuse un historique trop long, et couper au hasard sépare un
message assistant porteur de `tool_calls` de ses réponses `tool` — protocole
invalide. Ce module ne coupe donc qu'à une frontière de tour utilisateur : les
derniers tours restent intacts, les plus anciens sont résumés dans un message
`system`, et la consigne initiale avec la tâche d'origine ne disparaissent
jamais.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

__all__ = ["TrimResult", "plan_trim", "trim_messages"]

try:  # pragma: no cover - dépend de l'environnement
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001 - tiktoken optionnel
    _ENCODER = None

SUMMARY_MAX_CHARS = 2_000
_ITEM_MAX_CHARS = 180
#: Signatures d'appels d'outils reprises dans le résumé : au-delà, les plus
#: récentes suffisent à éviter une répétition.
_SIGNATURE_LIMIT = 8
_SIGNATURE_ITEM_MAX_CHARS = 160
_SIGNATURE_HEADER = "Appels d'outils déjà tentés (ne pas les répéter à l'identique) :"
_MESSAGE_OVERHEAD = 4
_LABELS = {"assistant": "AGENT", "tool": "OUTIL", "user": "UTILISATEUR", "system": "SYSTÈME"}


def count_tokens(text: str) -> int:
    """Tokens d'un texte (estimation à 4 caractères/token sans tiktoken)."""
    body = str(text or "")
    if _ENCODER is not None:
        return len(_ENCODER.encode(body, disallowed_special=()))
    return len(body) // 4


def _content_text(message: Mapping[str, Any]) -> str:
    """Texte comptabilisé d'un message, appels d'outils compris."""
    content = message.get("content")
    body = content if isinstance(content, str) else json.dumps(
        content, ensure_ascii=False, default=str
    )
    if message.get("tool_calls"):
        body += json.dumps(message["tool_calls"], ensure_ascii=False, default=str)
    return body


def messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Coût approximatif d'un historique, en tokens."""
    return sum(count_tokens(_content_text(m)) + _MESSAGE_OVERHEAD for m in messages)


def _tool_signatures(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Signatures `nom(arguments)` des appels de la zone repliée.

    Sans elles, le modèle oublie qu'il a déjà tenté une action et la répète
    jusqu'au garde-fou anti-boucle : le résumé doit porter les gestes, pas
    seulement les textes.
    """
    found: list[str] = []
    for message in reversed(messages):
        for call in reversed(message.get("tool_calls") or []):
            function = call.get("function") if isinstance(call, Mapping) else None
            function = function if isinstance(function, Mapping) else call
            name = str(function.get("name") or "?")
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False, default=str)
            arguments = " ".join(arguments.split())
            if len(arguments) > _SIGNATURE_ITEM_MAX_CHARS:
                arguments = arguments[:_SIGNATURE_ITEM_MAX_CHARS] + "…"
            found.append(f"- {name}({arguments})")
    unique = list(dict.fromkeys(found))
    return list(reversed(unique[:_SIGNATURE_LIMIT]))


def summarize(messages: Sequence[Mapping[str, Any]]) -> str:
    """Résumé ligne à ligne des messages repliés, borné en taille."""
    lines: list[str] = []
    for message in messages:
        body = " ".join(_content_text(message).split())
        if not body:
            continue
        if len(body) > _ITEM_MAX_CHARS:
            body = body[:_ITEM_MAX_CHARS] + "…"
        label = _LABELS.get(str(message.get("role", "")), "?")
        lines.append(f"- {label}: {body}")

    signatures = _tool_signatures(messages)
    block = f"\n{_SIGNATURE_HEADER}\n" + "\n".join(signatures) if signatures else ""
    # Les signatures passent en priorité : elles évitent une boucle, alors que
    # le détail ligne à ligne est du confort. On leur réserve la place d'abord.
    text = "\n".join(lines)
    room = SUMMARY_MAX_CHARS - len(block)
    if room <= 0:
        text = block[:_SIGNATURE_ITEM_MAX_CHARS]
    else:
        if len(text) > room:
            text = text[: max(0, room - 1)] + "…"
        text += block
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "\n…[résumé tronqué]"
    return text


@dataclass(slots=True)
class TrimResult:
    """Résultat d'un repli : l'historique retenu et de quoi l'expliquer."""

    messages: list[dict[str, Any]]
    before_messages: int
    after_messages: int
    before_tokens: int
    after_tokens: int
    summarized: int = 0
    reserved_tokens: int = 0

    @property
    def trimmed(self) -> bool:
        return self.after_messages < self.before_messages


def trim_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int,
    max_tokens: int = 0,
    reserved_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Comme `plan_trim`, mais ne retourne que l'historique retenu."""
    return plan_trim(
        messages,
        max_messages=max_messages,
        max_tokens=max_tokens,
        reserved_tokens=reserved_tokens,
    ).messages


def plan_trim(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int,
    max_tokens: int = 0,
    reserved_tokens: int = 0,
) -> TrimResult:
    """Retourne un historique conforme au budget, ou une copie inchangée.

    `reserved_tokens` compte ce qui accompagne chaque requête sans figurer
    dans `messages` : aujourd'hui les schémas d'outils. Le prompt système, lui,
    est déjà compté comme premier message.

    La consigne système et le premier tour utilisateur sont toujours conservés ;
    la coupe se fait juste avant un tour utilisateur, et les messages abandonnés
    sont résumés dans un message `system` inséré après la tâche d'origine.
    """
    items = list(messages)
    limit = max(4, int(max_messages))
    budget = max(0, int(max_tokens or 0))
    reserved = max(0, int(reserved_tokens or 0))
    before_tokens = messages_tokens(items)

    def result(retained: list[dict[str, Any]], summarized: int = 0) -> TrimResult:
        return TrimResult(
            messages=retained,
            before_messages=len(items),
            after_messages=len(retained),
            before_tokens=before_tokens,
            after_tokens=messages_tokens(retained),
            summarized=summarized,
            reserved_tokens=reserved,
        )

    if not items:
        return result([])

    def fits(candidate: list[dict[str, Any]]) -> bool:
        return len(candidate) <= limit and (
            not budget or messages_tokens(candidate) + reserved <= budget
        )

    if fits(items):
        return result(items)

    system = items[0]
    task = items[1] if len(items) > 1 and items[1].get("role") == "user" else {
        "role": "user",
        "content": "(historique tronqué)",
    }
    rest = items[2:]

    # Le premier message admissible donne la plus longue queue : on le garde
    # tant qu'il tient dans les deux budgets, puis on recule. Un message `tool`
    # ne peut pas ouvrir la queue (il répond à l'appel qui le précède) ; un
    # message assistant, si — c'est le cas courant d'une boucle d'outils avec
    # un seul tour utilisateur, où aucun tour `user` ne subsiste.
    starts = (index for index, m in enumerate(rest) if m.get("role") != "tool")
    for start in starts:
        candidate: list[dict[str, Any]] = [system, task]
        if start:
            candidate.append(
                {
                    "role": "system",
                    "content": (
                        "Résumé des tours précédents, repliés pour tenir dans le "
                        f"budget de contexte :\n{summarize(rest[:start])}"
                    ),
                }
            )
        candidate.extend(rest[start:])
        if fits(candidate):
            log.debug("Historique replié : %d message(s) résumé(s)", start)
            return result(candidate, summarized=start)

    return result([system, task])
