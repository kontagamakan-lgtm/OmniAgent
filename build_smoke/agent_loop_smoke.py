"""Smoke test autonome de la boucle ReAct du livrable 4.

Le scénario vérifie, sans réseau et sans toucher aux données de l'utilisateur :

* la planification facultative avant la boucle ;
* l'exécution d'un outil métier simulé ;
* la réponse finale ;
* l'auto-critique facultative, avec son événement.

Usage :
    python build_smoke/agent_loop_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Paths, Settings  # noqa: E402
from core.agent_core import AgentCore  # noqa: E402
from core.events import EventBus, EventType  # noqa: E402
from core.llm_client import LLMResponse  # noqa: E402
from core.react_parser import ParsedToolCall  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.base import FunctionTool, object_schema, param  # noqa: E402


class ScriptedLLM:
    """Fournisseur déterministe : chaque appel consomme la réponse suivante."""

    supports_stream = False

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if not self._script:
            raise AssertionError("script du faux fournisseur épuisé")
        return self._script.pop(0)

    def close(self) -> None:
        pass


def _settings(root: Path) -> Settings:
    """Réglages isolés : aucun chemin utilisateur, aucun budget ni réseau."""
    settings = Settings()
    settings.paths = Paths(
        install_dir=root,
        data_dir=root,
        config_file=root / "config.json",
        vault_file=root / "vault.enc",
        index_dir=root / "index",
        log_dir=root / "logs",
        download_dir=root / "downloads",
        cache_dir=root / "cache",
        report_dir=root / "reports",
        browser_profile_dir=root / "browser_profile",
    )
    settings.files.allowed_roots = [str(root)]
    settings.agent.max_steps = 4
    settings.agent.plan_before_act = True
    settings.agent.self_critique_passes = 1
    settings.budget.enabled = False
    settings.memory.enabled = False
    settings.obsidian.enabled = False
    settings.evolution.enabled = False
    settings.trace.enabled = False
    return settings


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="omniagent-smoke-") as tmp:
        root = Path(tmp)
        settings = _settings(root)
        seen: list[str] = []

        def echo(text: str = "") -> str:
            seen.append(text)
            return f"echo:{text}"

        registry = ToolRegistry(
            [
                FunctionTool(
                    "echo",
                    "Répète le texte fourni.",
                    object_schema({"text": param("string", "Texte", default="")}),
                    echo,
                )
            ]
        )
        llm = ScriptedLLM(
            [
                LLMResponse(
                    text='{"plan": ["appeler echo", "conclure"]}',
                    finish_reason="stop",
                ),
                LLMResponse(
                    text="",
                    tool_calls=[
                        ParsedToolCall(
                            name="echo",
                            arguments={"text": "bonjour"},
                            source="smoke",
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                LLMResponse(text="Écho exécuté : bonjour", finish_reason="stop"),
                LLMResponse(text='{"ok": true, "fix": ""}', finish_reason="stop"),
            ]
        )
        bus = EventBus()
        core = AgentCore(settings, llm, registry, bus=bus)

        reply = core.run("dis bonjour")

        assert reply.ok, reply.to_dict()
        assert "Écho exécuté : bonjour" in reply.text
        assert seen == ["bonjour"], seen
        assert bus.events(event_type=EventType.PLAN), "le plan doit être publié"
        assert bus.events(event_type=EventType.CRITIQUE), "la critique doit être publiée"
        assert bus.events(event_type=EventType.TOOL_FINISHED), "l'outil doit être exécuté"
        print("SMOKE_AGENT_OK")
        print(f"étapes={len(reply.steps)} outils={reply.tool_calls} requêtes={llm.calls}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
