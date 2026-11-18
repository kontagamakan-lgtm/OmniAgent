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
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

__all__ = ["count_tokens", "messages_tokens", "summarize", "trim_messages"]

try:  # pragma: no cover - dépend de l'environnement
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001 - tiktoken optionnel
    _ENCODER = None

SUMMARY_MAX_CHARS = 2_000
_ITEM_MAX_CHARS = 180
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
    text = "\n".join(lines)
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "\n…[résumé tronqué]"
    return text


def trim_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_messages: int,
    max_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Retourne un historique conforme au budget, ou une copie inchangée.

    La consigne système et le premier tour utilisateur sont toujours conservés ;
    la coupe se fait juste avant un tour utilisateur, et les messages abandonnés
    sont résumés dans un message `system` inséré après la tâche d'origine.
    """
    items = list(messages)
    if not items:
        return []

    limit = max(4, int(max_messages))
    budget = max(0, int(max_tokens or 0))
    if len(items) <= limit and (not budget or messages_tokens(items) <= budget):
        return items

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
        if len(candidate) <= limit and (not budget or messages_tokens(candidate) <= budget):
            log.debug("Historique replié : %d message(s) résumé(s)", start)
            return candidate

    return [system, task]
