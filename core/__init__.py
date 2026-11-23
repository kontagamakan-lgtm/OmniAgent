"""Cœur de l'agent OmniAgent : boucle ReAct, LLM, outils, événements.

Les symboles publics sont résolus à la première lecture (PEP 562) au lieu
d'être importés ici. Un `__init__` qui chargeait `core.factory` et
`core.runner` importait par ricochet `tools.browser_agent`, donc
`tools.file_manager` ; or ce dernier importe `core.file_tx`, qui déclenche
l'initialisation du paquet `core` : le module `tools.file_manager` était alors
à moitié construit et l'import échouait. Importer un sous-module ne doit
charger que lui, jamais la fabrique complète.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Module qui définit chaque symbole réexporté.
_MODULES: dict[str, tuple[str, ...]] = {
    "core.agent_core": ("AgentCore", "AgentReply", "AgentStep", "StepCall"),
    "core.errors": (
        "AgentError",
        "BudgetExhausted",
        "Cancelled",
        "ConfigError",
        "LLMAuthError",
        "LLMError",
        "LLMRateLimited",
        "LLMResponseError",
        "LoopGuardTripped",
        "RecoveryExhausted",
        "ToolCallError",
    ),
    "core.events": ("Event", "EventBus", "EventType"),
    "core.factory": ("AgentBundle", "build_agent"),
    "core.llm_client": ("LLMClient", "LLMResponse"),
    "core.react_parser": ("ParsedToolCall", "parse_tool_calls"),
    "core.runner": ("AgentResult", "AgentRunner", "run_task"),
    "core.tool_registry": ("ToolError", "ToolRegistry", "ToolSpec"),
}

#: Symbole -> module d'origine, pour la résolution paresseuse.
_EXPORTS: dict[str, str] = {
    name: module for module, names in _MODULES.items() for name in names
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Importe le module d'un symbole au premier accès, puis le met en cache."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} n'a pas d'attribut {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
