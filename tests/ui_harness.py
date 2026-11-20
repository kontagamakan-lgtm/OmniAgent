"""Construction partagée d'une vraie fenêtre OmniAgent pour les tests UI."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from support import make_settings


class UnusedLLM:
    supports_stream = False

    def complete(self, *_args, **_kwargs):
        raise AssertionError("aucun appel LLM dans les tests de géométrie")

    def close(self) -> None:
        pass


def settings_for_ui(root: Path, *, font_size: int = 13):
    settings = make_settings(root)
    settings.llm.backend = "ollama"
    settings.ui.font_size = font_size
    settings.budget.enabled = False
    settings.trace.enabled = False
    settings.memory.enabled = False
    settings.obsidian.enabled = False
    settings.evolution.enabled = False
    return settings


def make_window(settings):
    from core.agent_core import AgentCore
    from core.tool_registry import ToolRegistry
    from ui.app_gui import OmniAgentWindow

    registry = ToolRegistry([])
    agent = AgentCore(settings, UnusedLLM(), registry)
    bundle = SimpleNamespace(
        agent=agent,
        registry=registry,
        evolver=None,
        settings=settings,
    )
    return OmniAgentWindow(settings, bundle)


def settle(window: object, *, rounds: int = 3, delay: float = 0.08) -> None:
    for _ in range(rounds):
        window.update_idletasks()
        window.update()
        time.sleep(delay)
    window.update()
