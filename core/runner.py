"""Façade d'exécution d'une tâche : un agent complet, un résultat, rien de plus.

`AgentCore` demande un registre et un client déjà construits ; `AgentRunner`
assemble les mêmes ressources via `build_agent`, les referme après usage, et
expose `run()`. `run_task` fait le trajet complet pour un appel ponctuel.
"""

from __future__ import annotations

from typing import Any

from config import Settings
from core.agent_core import AgentReply
from core.events import EventBus
from core.factory import build_agent
from core.llm_client import LLMClient
from core.tool_registry import ConfirmHandler

__all__ = ["AgentResult", "AgentRunner", "run_task"]

# Nom historique du résultat d'exécution.
AgentResult = AgentReply


class AgentRunner:
    """Exécute des tâches sur un agent assemblé une fois."""

    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        bus: EventBus | None = None,
        confirm: ConfirmHandler | None = None,
        with_web: bool = True,
        with_browser: bool = True,
    ) -> None:
        self.bundle = build_agent(
            settings,
            llm=llm,
            bus=bus,
            confirm=confirm,
            with_web=with_web,
            with_browser=with_browser,
        )

    def run(self, task: str, *, max_steps: int | None = None) -> AgentResult:
        """Exécute une tâche et retourne son résultat complet."""
        return self.bundle.agent.run(task, max_steps=max_steps)

    def close(self) -> None:
        self.bundle.close()

    def __enter__(self) -> "AgentRunner":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def run_task(task: str, *, settings: Settings | None = None, **options: Any) -> AgentResult:
    """Assemble un agent, exécute la tâche, referme les ressources.

    `options` sont ceux de `AgentRunner` (`llm`, `bus`, `confirm`, `with_web`,
    `with_browser`).
    """
    with AgentRunner(settings or Settings.load(), **options) as runner:
        return runner.run(task)
