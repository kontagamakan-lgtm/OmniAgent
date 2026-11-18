"""Tests du cœur ReAct : boucle, budget, reprises, registre, client LLM."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from support import make_settings, run, tmpdir  # noqa: I001

import config
from core.agent_core import AgentCore
from core.errors import (
    LLMAuthError,
    LLMError,
    LLMRateLimited,
    LLMResponseError,
    retry_delay,
)
from core.events import EventBus, EventType
from core.llm_client import LLMClient, LLMResponse
from core.prompts import FINAL_ANSWER_PREFIX, system_prompt, tool_catalogue
from core.react_parser import (
    ParsedToolCall,
    decode_arguments,
    parse_model_json,
    parse_tool_calls,
)
from core.tool_registry import ToolRegistry
from tools.base import FunctionTool, ToolResult, object_schema, param


# ---------------------------------------------------------------------------
# Doublures
# ---------------------------------------------------------------------------


class FakeLLM:
    """Fournisseur scripté : une entrée de `script` par appel à `complete`."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.script:
            raise AssertionError("script du faux LLM épuisé")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def last_tool_contents(self) -> list[str]:
        return [
            str(message.get("content"))
            for message in self.calls[-1]["messages"]
            if message.get("role") == "tool"
        ]


def _call(name: str, **arguments) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[ParsedToolCall(name=name, arguments=dict(arguments), source="test")],
        finish_reason="tool_calls",
    )


def _text(value: str) -> LLMResponse:
    return LLMResponse(text=value, finish_reason="stop")


def _echo_tool(store: list[str]) -> FunctionTool:
    def echo(text: str = "") -> str:
        store.append(text)
        return f"echo:{text}"

    return FunctionTool(
        "echo",
        "Répète le texte fourni.",
        object_schema({"text": param("string", "Texte à répéter", default="")}),
        echo,
    )


def _secret_tool() -> FunctionTool:
    def reveal() -> ToolResult:
        return ToolResult(
            ok=True,
            data={"password": "SuperSecret#42"},
            sensitive=True,
            safe_summary="secret disponible en local ; valeur non transmise au modèle.",
        )

    return FunctionTool("reveal", "Renvoie un secret.", object_schema({}), reveal)


def _boom_tool() -> FunctionTool:
    def boom() -> None:
        raise ValueError("explosion contrôlée")

    return FunctionTool("boom", "Échoue toujours.", object_schema({}), boom)


def _settings(tmp, **agent_overrides):
    settings = make_settings(tmp)
    settings.agent.max_steps = agent_overrides.pop("max_steps", 6)
    settings.agent.max_recovery_attempts = agent_overrides.pop("max_recovery", 2)
    settings.agent.history_max_messages = agent_overrides.pop("history_max", 40)
    settings.llm.max_retries = 0
    for key, value in agent_overrides.items():
        setattr(settings.agent, key, value)
    return settings


# ---------------------------------------------------------------------------
# Boucle ReAct
# ---------------------------------------------------------------------------


def test_react_loop_runs_tool_then_answers() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        seen: list[str] = []
        registry = ToolRegistry([_echo_tool(seen)])
        llm = FakeLLM([_call("echo", text="bonjour"), _text(f"{FINAL_ANSWER_PREFIX} terminé")])
        core = AgentCore(settings, llm, registry)

        reply = core.run("dis bonjour")

        assert reply.ok and reply.stop_reason == "final", reply.to_dict()
        assert reply.text == "terminé"
        assert reply.tool_calls == 1
        assert seen == ["bonjour"]
        assert [step.index for step in reply.steps] == [1, 2]
        assert llm.last_tool_contents() == ["echo:bonjour"]
        assert llm.calls[0]["tools"], "les schémas d'outils doivent être transmis"
        assert llm.calls[0]["messages"][0]["role"] == "system"
        assert "echo" in llm.calls[0]["messages"][0]["content"]

        types = [event.type for event in core.bus.events()]
        assert types[0] == EventType.RUN_STARTED
        assert EventType.TOOL_STARTED in types and EventType.TOOL_FINISHED in types
        assert types[-1] == EventType.RUN_FINISHED


def test_assistant_message_lists_only_executed_tool_calls() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        settings.agent.parallel_tool_calls = 1
        seen: list[str] = []
        both = LLMResponse(
            text="",
            tool_calls=[
                ParsedToolCall(name="echo", arguments={"text": "a"}, source="test"),
                ParsedToolCall(name="echo", arguments={"text": "b"}, source="test"),
            ],
            finish_reason="tool_calls",
        )
        llm = FakeLLM([both, _text("fini")])
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("deux appels")
        assert reply.ok
        assert seen == ["a"], "un seul appel doit être exécuté"

        assistant = [m for m in llm.calls[-1]["messages"] if m.get("role") == "assistant"][-1]
        assert len(assistant["tool_calls"]) == 1, assistant
        tool_messages = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == [
            call["id"] for call in assistant["tool_calls"]
        ]


def test_generated_tool_call_ids_are_unique() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        settings.agent.parallel_tool_calls = 3
        seen: list[str] = []
        both = LLMResponse(
            text="",
            tool_calls=[
                ParsedToolCall(name="echo", arguments={"text": "a"}, source="test"),
                ParsedToolCall(name="echo", arguments={"text": "b"}, source="test"),
            ],
            finish_reason="tool_calls",
        )
        llm = FakeLLM([both, _text("fini")])
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("deux appels")
        assert reply.ok and seen == ["a", "b"]

        messages = llm.calls[-1]["messages"]
        assistant = [m for m in messages if m.get("role") == "assistant"][-1]
        ids = [call["id"] for call in assistant["tool_calls"]]
        assert len(ids) == 2 and len(set(ids)) == 2, ids
        assert [m["tool_call_id"] for m in messages if m.get("role") == "tool"] == ids


def test_sensitive_tool_result_never_reaches_the_model() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        llm = FakeLLM([_call("reveal"), _text("fait")])
        core = AgentCore(settings, llm, ToolRegistry([_secret_tool()]))

        reply = core.run("lis le secret")
        assert reply.ok
        observation = llm.last_tool_contents()[-1]
        assert "SuperSecret#42" not in observation
        assert "non transmise" in observation


def test_tool_failure_is_fed_back_not_fatal() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        llm = FakeLLM([_call("boom"), _text("j'ai échoué")])
        core = AgentCore(settings, llm, ToolRegistry([_boom_tool()]))

        reply = core.run("essaie")
        assert reply.ok and reply.stop_reason == "final"
        observation = llm.last_tool_contents()[-1]
        assert "ERREUR" in observation and "explosion contrôlée" in observation


def test_budget_exhaustion_closes_with_a_summary() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=2)
        seen: list[str] = []
        llm = FakeLLM(
            [
                _call("echo", text="un"),
                _call("echo", text="deux"),
                _text("bilan partiel"),
            ]
        )
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("boucle")
        assert reply.stop_reason == "budget"
        assert reply.text == "bilan partiel"
        assert len(reply.steps) == 3  # deux étapes d'outils + la synthèse
        assert llm.calls[-1]["tools"] is None, "la synthèse finale ne doit pas proposer d'outil"


def test_budget_synthesis_failure_is_reported() -> None:
    with tmpdir("core") as tmp:
        # Budget de reprises à zéro : la panne de synthèse est rapportée telle
        # quelle, sans nouvelle tentative (voir les tests de reprise dédiés).
        settings = _settings(tmp, max_steps=2, max_recovery=0)
        seen: list[str] = []
        llm = FakeLLM(
            [
                _call("echo", text="un"),
                _call("echo", text="deux"),
                LLMError("synthèse impossible"),
            ]
        )
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("budget puis panne")

        assert reply.ok is False
        assert reply.stop_reason == "budget"
        assert reply.error_code == "budget_exhausted"
        assert "synthèse impossible" in reply.text
        assert "Limite d'étapes" in reply.text
        assert reply.tool_calls == 2
        assert reply.recoveries == 0
        assert len(reply.steps) == 2, "aucune étape inventée pour la synthèse échouée"
        assert seen == ["un", "deux"]
        assert sum(1 for call in llm.calls if call["tools"] is None) == 1

        types = [event.type for event in core.bus.events()]
        assert types[-1] == EventType.RUN_FINISHED


def test_budget_synthesis_retries_transient_errors() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=2, max_recovery=2)
        seen: list[str] = []
        delays: list[float] = []
        llm = FakeLLM(
            [
                _call("echo", text="un"),
                _call("echo", text="deux"),
                LLMError("réseau instable"),
                _text("bilan final"),
            ]
        )
        core = AgentCore(
            settings, llm, ToolRegistry([_echo_tool(seen)]), sleep=delays.append
        )

        reply = core.run("budget puis incident réseau")

        assert reply.ok is True, reply.to_dict()
        assert reply.stop_reason == "budget"
        assert reply.text == "bilan final"
        assert reply.recoveries == 1
        assert len(delays) == 1 and delays[0] > 0, delays
        # Les deux dernières tentatives sont la même demande de synthèse.
        assert [call["tools"] for call in llm.calls[-2:]] == [None, None]
        assert seen == ["un", "deux"]

        retries = [event for event in core.bus.events() if event.type == EventType.LLM_RETRY]
        assert len(retries) == 1
        assert retries[0].payload["closing"] is True
        assert retries[0].payload["attempt"] == 1


def test_cancel_during_budget_synthesis_is_not_a_budget_failure() -> None:
    """Annuler pendant la synthèse de clôture doit donner « cancelled »."""
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=2)
        seen: list[str] = []
        llm = FakeLLM(
            [_call("echo", text="un"), _call("echo", text="deux"), _text("bilan ignoré")]
        )
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        original = llm.complete

        def cancel_then_answer(messages, tools=None, **kwargs):
            # La synthèse (seul appel sans outils) est lancée, puis l'utilisateur
            # clique sur « Annuler » avant que le fournisseur n'ait rendu la main.
            if tools is None:
                core.cancel()
            return original(messages, tools=tools, **kwargs)

        llm.complete = cancel_then_answer

        reply = core.run("budget puis annulation")

        assert reply.stop_reason == "cancelled", reply.to_dict()
        assert reply.error_code == "cancelled"
        assert reply.ok is False
        assert "Limite d'étapes" not in reply.text
        assert reply.text == "Exécution interrompue à la demande de l'utilisateur."
        assert reply.tool_calls == 2
        assert len(reply.steps) == 2, "aucune étape de synthèse ajoutée"

        types = [event.type for event in core.bus.events()]
        assert EventType.FINAL not in types
        assert types[-1] == EventType.RUN_FINISHED


def test_budget_synthesis_retries_even_after_loop_recoveries() -> None:
    """La clôture a son propre décompte : les reprises des étapes ne la privent pas."""
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=4, max_recovery=2)
        seen: list[str] = []
        delays: list[float] = []
        llm = FakeLLM(
            [
                LLMError("panne 1"),
                _call("echo", text="un"),
                LLMError("panne 2"),
                _call("echo", text="deux"),
                LLMError("panne 3 (synthèse)"),
                _text("bilan après reprises"),
            ]
        )
        core = AgentCore(
            settings, llm, ToolRegistry([_echo_tool(seen)]), sleep=delays.append
        )

        reply = core.run("budget, pannes, puis bilan")

        assert reply.ok is True, reply.to_dict()
        assert reply.stop_reason == "budget"
        assert reply.text == "bilan après reprises"
        # Deux reprises consommées par la boucle, une par la synthèse.
        assert reply.recoveries == 3
        assert len(delays) == 3, delays
        assert seen == ["un", "deux"]

        retries = [event for event in core.bus.events() if event.type == EventType.LLM_RETRY]
        closing = [event for event in retries if event.payload.get("closing")]
        assert len(closing) == 1
        assert closing[0].payload["attempt"] == 1, "décompte propre à la clôture"


def test_budget_synthesis_stops_after_recovery_budget() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=2, max_recovery=1)
        seen: list[str] = []
        llm = FakeLLM(
            [
                _call("echo", text="un"),
                _call("echo", text="deux"),
                LLMError("première panne"),
                LLMError("seconde panne"),
            ]
        )
        core = AgentCore(
            settings, llm, ToolRegistry([_echo_tool(seen)]), sleep=lambda _s: None
        )

        reply = core.run("budget puis pannes répétées")

        assert reply.ok is False
        assert reply.stop_reason == "budget"
        assert reply.error_code == "budget_exhausted"
        assert reply.recoveries == 1
        assert "seconde panne" in reply.text, reply.text
        assert "Limite d'étapes" in reply.text
        assert [call["tools"] for call in llm.calls[-2:]] == [None, None]
        assert len(reply.steps) == 2, "une synthèse échouée n'ajoute pas d'étape"


def test_budget_synthesis_exhaustion_is_requalified_like_the_loop() -> None:
    """Les reprises de la clôture s'épuisent et se qualifient comme celles de la boucle."""
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=2, max_recovery=1)
        seen: list[str] = []
        llm = FakeLLM(
            [
                _call("echo", text="un"),
                _call("echo", text="deux"),
                LLMError("première panne"),
                LLMError("seconde panne"),
            ]
        )
        core = AgentCore(
            settings, llm, ToolRegistry([_echo_tool(seen)]), sleep=lambda _s: None
        )

        reply = core.run("budget puis pannes répétées")

        assert reply.ok is False
        assert reply.stop_reason == "budget"
        assert reply.error_code == "budget_exhausted"
        assert reply.recoveries == 1
        # Même formulation que l'épuisement des reprises dans la boucle.
        assert reply.error == "1 reprise(s) sans succès : seconde panne", reply.error
        assert "1 reprise(s) sans succès" in reply.text
        assert "Limite d'étapes" in reply.text
        assert len(reply.steps) == 2, "une synthèse échouée n'ajoute pas d'étape"

        retries = [event for event in core.bus.events() if event.type == EventType.LLM_RETRY]
        assert len(retries) == 1
        assert retries[0].payload["closing"] is True
        assert retries[0].payload["attempt"] == 1


def test_retryable_llm_error_is_retried_with_backoff() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_recovery=2)
        delays: list[float] = []
        llm = FakeLLM([LLMError("réseau instable"), _text("réponse")])
        core = AgentCore(
            settings,
            llm,
            ToolRegistry([_echo_tool([])]),
            sleep=delays.append,
        )

        reply = core.run("test")
        assert reply.ok and reply.stop_reason == "final"
        assert reply.recoveries == 1
        assert delays == [settings.llm.retry_backoff_s]
        types = [event.type for event in core.bus.events()]
        assert EventType.LLM_RETRY in types


def test_recovery_budget_exhaustion_is_reported() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_recovery=2)
        llm = FakeLLM([LLMError("réseau instable") for _ in range(3)])
        core = AgentCore(
            settings, llm, ToolRegistry([_echo_tool([])]), sleep=lambda _s: None
        )

        reply = core.run("test")
        assert reply.ok is False
        assert reply.error_code == "recovery_exhausted"
        assert reply.recoveries == 2
        assert "réseau instable" in reply.error
        assert len(llm.calls) == 3, "deux reprises puis l'échec, pas davantage"


def test_factory_wires_real_tools_and_reads_a_file() -> None:
    from core.factory import build_agent

    with tmpdir("factory") as tmp:
        settings = _settings(tmp)
        target = tmp / "note.txt"
        target.write_text("CONTENU REEL DU FICHIER", encoding="utf-8")

        llm = FakeLLM([_call("fs_read_text", path=str(target)), _text("lu")])
        bundle = build_agent(settings, llm=llm, with_web=False, with_browser=False)
        try:
            names = bundle.tool_names()
            assert "fs_read_text" in names and "vault_status" in names
            assert "web_search" not in names and "browser_open" not in names

            schemas = bundle.registry.schemas()
            assert {schema["function"]["name"] for schema in schemas} == set(names)
            assert all(
                schema["type"] == "function" and schema["function"]["parameters"]
                for schema in schemas
            )

            # Lecture réelle, hors boucle : le gestionnaire de fichiers est branché.
            direct = bundle.registry.dispatch("fs_read_text", {"path": str(target)})
            assert direct.ok, direct.error
            assert direct.data["text"] == "CONTENU REEL DU FICHIER"

            # Puis dans la boucle ReAct : l'observation contient le fichier.
            reply = bundle.agent.run("lis le fichier")
            assert reply.ok and reply.text == "lu", reply.to_dict()
            context = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
            assert "CONTENU REEL DU FICHIER" in context
        finally:
            bundle.close()


def test_factory_registers_web_and_browser_tools() -> None:
    from core.factory import build_agent

    with tmpdir("factory") as tmp:
        bundle = build_agent(_settings(tmp), llm=FakeLLM([]))
        try:
            names = set(bundle.tool_names())
            assert {"web_search", "web_fetch", "browser_open", "login_to_service"} <= names
            assert "browser_fill_signup" in names and "vault_create_credential" in names
            assert bundle.searcher is not None and bundle.browser is not None
            assert len(bundle.registry) == len(names)
        finally:
            # Ne doit pas lever même si ni le navigateur ni le réseau n'ont servi.
            bundle.close()
            bundle.close()


def test_core_package_exports_are_resolvable() -> None:
    import importlib

    package = importlib.import_module("core")
    assert package.__all__, "core.__all__ ne doit pas être vide"
    assert len(package.__all__) == len(set(package.__all__)), "doublon dans __all__"

    missing = sorted(name for name in package.__all__ if not hasattr(package, name))
    assert missing == [], f"symboles déclarés mais absents : {missing}"

    # `from core import *` doit exposer exactement les mêmes objets.
    namespace: dict[str, object] = {}
    exec("from core import *", namespace)  # noqa: S102 - test d'API
    for name in package.__all__:
        assert namespace[name] is getattr(package, name), name

    # Un réexport doit pointer vers la définition d'origine, pas vers une copie.
    for name in package.__all__:
        value = getattr(package, name)
        module = getattr(value, "__module__", "")
        if isinstance(value, type) and module.startswith("core."):
            assert getattr(importlib.import_module(module), name, value) is value, name

    assert package.StepCall is importlib.import_module("core.agent_core").StepCall
    assert package.AgentCore.__module__ == "core.agent_core"
    assert package.RecoveryExhausted.code == "recovery_exhausted"


def test_non_retryable_error_stops_immediately() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        llm = FakeLLM([LLMAuthError("cle invalide")])
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool([])]))

        reply = core.run("test")
        assert reply.ok is False and reply.error_code == "llm_auth"
        assert reply.recoveries == 0
        assert "cle invalide" in reply.text


def test_loop_guard_stops_repeated_action() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, max_steps=10)
        seen: list[str] = []
        llm = FakeLLM([_call("echo", text="encore") for _ in range(10)])
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("tourne en rond")
        assert reply.stop_reason == "loop_guard"
        assert reply.ok is False
        # Trois exécutions autorisées, la quatrième est refusée avant d'agir.
        assert seen == ["encore", "encore", "encore"]
        assert reply.tool_calls == 3


def test_cancel_during_run_stops_before_next_step() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        holder: dict = {}

        def cancelling_echo(text: str = "") -> str:
            holder["core"].cancel()
            return f"echo:{text}"

        tool = FunctionTool(
            "echo",
            "Répète le texte fourni.",
            object_schema({"text": param("string", "Texte à répéter", default="")}),
            cancelling_echo,
        )
        llm = FakeLLM([_call("echo", text="stop"), _text("jamais atteint")])
        core = AgentCore(settings, llm, ToolRegistry([tool]))
        holder["core"] = core

        reply = core.run("test")
        assert reply.stop_reason == "cancelled"
        assert reply.ok is False
        assert reply.tool_calls == 1, "l'outil en cours doit se terminer"
        assert len(llm.calls) == 1, "aucune étape supplémentaire ne doit partir"


def test_cancel_is_reset_at_the_start_of_a_run() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp)
        llm = FakeLLM([_text("ok")])
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool([])]))
        core.cancel()
        assert core.cancelled is True

        reply = core.run("nouvelle tâche")
        assert reply.ok and reply.stop_reason == "final", reply.to_dict()
        assert core.cancelled is False


def test_empty_task_is_rejected_without_calling_the_llm() -> None:
    with tmpdir("core") as tmp:
        llm = FakeLLM([])
        core = AgentCore(_settings(tmp), llm, ToolRegistry([]))
        reply = core.run("   ")
        assert reply.ok is False and reply.error_code == "empty_task"
        assert llm.calls == []


def test_history_is_trimmed_on_turn_boundaries() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, history_max=6)
        seen: list[str] = []
        script = []
        for index in range(6):
            script.append(_call("echo", text=f"t{index}"))
        script.append(_text("fini"))
        llm = FakeLLM(script)
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("longue tâche")
        assert reply.ok
        messages = llm.calls[-1]["messages"]
        assert messages[0]["role"] == "system"
        assert len(messages) <= 6
        assert messages[1]["role"] == "user", "l'historique doit commencer par un tour utilisateur"
        assert all(message["role"] != "tool" or index > 1 for index, message in enumerate(messages))


# ---------------------------------------------------------------------------
# Registre
# ---------------------------------------------------------------------------


def test_trimming_keeps_the_original_task() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, history_max=4)
        seen: list[str] = []
        script = [_call("echo", text=f"t{index}") for index in range(8)] + [_text("fini")]
        llm = FakeLLM(script)
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("LA TÂCHE ORIGINALE")
        assert reply.ok
        messages = llm.calls[-1]["messages"]
        assert messages[0]["role"] == "system"
        assert len(messages) <= 4
        # La consigne ne doit jamais disparaître, même si la fenêtre n'a pas de tour user.
        assert messages[1]["role"] == "user"
        assert "LA TÂCHE ORIGINALE" in str(messages[1]["content"])


def test_trimming_mid_loop_keeps_a_summary_without_a_user_turn() -> None:
    """Boucle d'outils à un seul tour user : le repli résume au lieu de tout jeter."""
    with tmpdir("core") as tmp:
        settings = _settings(tmp, history_max=6)
        seen: list[str] = []
        script = [_call("echo", text=f"t{index}") for index in range(5)] + [_text("fini")]
        llm = FakeLLM(script)
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        assert core.run("TÂCHE UNIQUE").ok

        # 4e appel : l'historique a dépassé 6 messages et vient d'être replié.
        messages = llm.calls[3]["messages"]
        assert len(messages) <= 6
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user" and "TÂCHE UNIQUE" in str(messages[1]["content"])
        summaries = [message for message in messages[2:] if message["role"] == "system"]
        assert summaries, "un tour abandonné doit être résumé, même sans tour user"
        assert "- OUTIL: echo" in str(summaries[0]["content"])
        assert messages[2]["role"] != "tool", "la queue ne peut pas ouvrir sur un résultat d'outil"


def test_context_budget_folds_old_turns_into_a_summary() -> None:
    with tmpdir("core") as tmp:
        settings = _settings(tmp, history_max=8, max_context_tokens=1500)
        seen: list[str] = []
        script = [
            _call("echo", text=f"t{index} " + "x" * 400) for index in range(10)
        ] + [_text("fini")]
        llm = FakeLLM(script)
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool(seen)]))

        reply = core.run("TÂCHE LONGUE")

        assert reply.ok, reply.to_dict()
        messages = llm.calls[-1]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "TÂCHE LONGUE" in str(messages[1]["content"])
        summaries = [message for message in messages[2:] if message["role"] == "system"]
        assert summaries, "le repli doit laisser un résumé des tours abandonnés"
        assert "- OUTIL: echo" in str(summaries[0]["content"])
        # Un message `tool` ne doit jamais ouvrir l'historique tronqué.
        assert messages[2]["role"] != "tool"


def test_parse_model_json_reads_fenced_and_noisy_output() -> None:
    assert parse_model_json('```json\n{"action": "list"}\n```') == {"action": "list"}
    assert parse_model_json(
        'Bien sûr : {"action": {"nested": "}"}} et voilà.'
    ) == {"action": {"nested": "}"}}
    assert parse_model_json("aucun objet ici") is None
    assert parse_model_json("[1, 2, 3]") is None


def test_dangerous_tool_requires_confirmation() -> None:
    with tmpdir("core"):
        executed: list[int] = []

        def wipe() -> str:
            executed.append(1)
            return "effacé"

        tool = FunctionTool(
            "wipe",
            "Efface tout.",
            object_schema({}),
            wipe,
            read_only=False,
            dangerous=True,
        )

        refused = ToolRegistry([tool])
        result = refused.dispatch("wipe")
        assert result.ok is False and result.error_code == "confirmation_required"
        assert executed == []

        allowed = ToolRegistry([tool], confirm=lambda t, args: True)
        assert allowed.dispatch("wipe").ok is True
        assert executed == [1]

        denied = ToolRegistry([tool], confirm=lambda t, args: False)
        assert denied.dispatch("wipe").error_code == "confirmation_required"

        # Une exception du confirmateur vaut refus, pas plantage.
        def broken(t, args):
            raise RuntimeError("interface absente")

        assert ToolRegistry([tool], confirm=broken).dispatch("wipe").error_code == (
            "confirmation_required"
        )


def test_registry_truncates_and_counts() -> None:
    with tmpdir("core"):
        def flood() -> dict:
            return {"texte": "x" * 500}

        registry = ToolRegistry(
            [FunctionTool("flood", "Renvoie beaucoup.", object_schema({}), flood)],
            max_output_chars=80,
        )
        result = registry.dispatch("flood")
        observation = registry.observation(result)
        assert observation.startswith('{"texte"') and "tronqué" in observation

        registry.dispatch("flood")
        registry.dispatch("absent")
        stats = registry.stats()
        assert stats["flood"]["calls"] == 2
        assert "absent" not in stats


# ---------------------------------------------------------------------------
# Parseur
# ---------------------------------------------------------------------------


def test_parser_handles_every_documented_format() -> None:
    tagged = parse_tool_calls('<tool_call>{"name": "fs_list", "arguments": {"path": "."}}</tool_call>')
    assert tagged[0].name == "fs_list" and tagged[0].arguments == {"path": "."}

    fenced = parse_tool_calls('Je liste.\n```json\n{"tool": "fs_list", "args": {}}\n```')
    assert fenced[0].name == "fs_list"

    inline = parse_tool_calls('Je vais chercher {"name": "web_search", "arguments": {"query": "x"}} ici.')
    assert inline[0].name == "web_search" and inline[0].arguments == {"query": "x"}

    nested = parse_tool_calls('{"function": {"name": "echo", "arguments": "{\\"text\\": \\"salut\\"}"}}')
    assert nested[0].arguments == {"text": "salut"}

    payload = json.dumps(
        [{"name": "a", "arguments": {}}, {"name": "a", "arguments": {}}, {"name": "b", "arguments": {}}]
    )
    assert [call.name for call in parse_tool_calls(payload)] == ["a", "b"]

    assert parse_tool_calls("aucun appel ici") == []
    assert parse_tool_calls("") == []

    try:
        decode_arguments("{pas du json")
    except Exception as exc:
        assert "illisibles" in str(exc)
    else:
        raise AssertionError("des arguments illisibles doivent lever")


def test_react_parser_rejects_bad_names() -> None:
    assert parse_tool_calls('{"name": "rm -rf /", "arguments": {}}') == []
    assert parse_tool_calls('{"name": "", "arguments": {}}') == []


# ---------------------------------------------------------------------------
# Client LLM
# ---------------------------------------------------------------------------


def _client(handler, *, backend="deepseek", **cfg_overrides) -> LLMClient:
    cfg = config.LLMConfig()
    cfg.backend = backend
    cfg.api_key_env = "TEST_LLM_KEY"
    cfg.max_retries = 0
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return LLMClient(cfg, client=client, sleep=lambda _s: None)


def test_llm_client_stream_complete_feeds_chunks_and_tool_calls(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        lines = [
            'data: {"choices": [{"delta": {"content": "Bonjour "}}]}',
            'data: {"choices": [{"delta": {"content": "le "}}]}',
            'data: {"choices": [{"delta": {"content": "monde"}}]}',
            'data: {"choices": [{"delta": {"tool_calls": ['
            '{"index": 0, "id": "c1", "function": {"name": "echo", "arguments": "{\\"te"}}]}}]}',
            'data: {"choices": [{"delta": {"tool_calls": ['
            '{"index": 0, "function": {"arguments": "xt\\": \\"salut\\"}"}}]}}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}',
            "data: [DONE]",
        ]
        body = "\n\n".join(lines) + "\n\n"
        return httpx.Response(
            200, content=body.encode("utf-8"), headers={"content-type": "text/event-stream"}
        )

    client = _client(handler)
    chunks: list[str] = []
    response = client.stream_complete(
        [{"role": "user", "content": "salue"}], on_token=chunks.append
    )

    assert captured[0]["stream"] is True
    assert chunks == ["Bonjour ", "le ", "monde"]
    assert response.text == "Bonjour le monde"
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.name, call.arguments, call.call_id) == ("echo", {"text": "salut"}, "c1")


class _SlowStream:
    """Serveur SSE local qui débite lentement : l'attente devient mesurable."""

    def __init__(self, chunks: int = 40, delay_s: float = 0.1) -> None:
        self.chunks = chunks
        self.delay_s = delay_s
        self.sent = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - nom imposé par http.server
                self.rfile.read(int(self.headers.get("content-length") or 0))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                try:
                    for index in range(outer.chunks):
                        line = 'data: {"choices": [{"delta": {"content": "m%d "}}]}\n\n'
                        self.wfile.write((line % index).encode("utf-8"))
                        self.wfile.flush()
                        outer.sent += 1
                        time.sleep(outer.delay_s)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "_SlowStream":
        self.thread.start()
        return self

    def __exit__(self, *info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_abort_closes_the_stream_without_waiting_for_the_provider(monkeypatch) -> None:
    """Sans `on_token`, seule la fermeture du flux peut rendre la main tôt."""
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    with _SlowStream(chunks=40, delay_s=0.1) as server:
        cfg = config.LLMConfig()
        cfg.backend = "deepseek"
        cfg.base_url = server.url
        cfg.api_key_env = "TEST_LLM_KEY"
        cfg.max_retries = 0
        client = LLMClient(cfg)

        timer = threading.Timer(0.3, client.abort)
        timer.start()
        started = time.monotonic()
        error: LLMError | None = None
        try:
            client.stream_complete([{"role": "user", "content": "parle longtemps"}])
        except LLMError as exc:
            error = exc
        elapsed = time.monotonic() - started
        timer.join(timeout=5)
        client.close()

        assert error is not None, "le flux aurait dû être interrompu"
        assert elapsed < 1.5, f"{elapsed:.2f} s : le flux n'a pas été fermé"
        assert error.code == "llm_aborted"
        assert error.retryable is False, "une annulation n'est pas une panne"
        assert server.sent < 40


def test_cancel_interrupts_the_in_flight_stream(monkeypatch) -> None:
    """Annuler pendant la génération ferme le flux au lieu d'attendre sa fin."""
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    # 40 fragments à 0,1 s : ~4 s si l'on attendait la requête complète.
    with tmpdir("core") as tmp, _SlowStream(chunks=40, delay_s=0.1) as server:
        settings = _settings(tmp)
        settings.llm.backend = "deepseek"
        settings.llm.base_url = server.url
        settings.llm.api_key_env = "TEST_LLM_KEY"
        settings.llm.max_retries = 0
        llm = LLMClient(settings.llm)
        core = AgentCore(settings, llm, ToolRegistry([_echo_tool([])]))

        cancel_thread = threading.Thread(target=core.cancel)
        chunks: list[str] = []

        def stop_on_first_chunk(event) -> None:
            chunks.append(str(event.payload["text"]))
            if not core.cancelled and not cancel_thread.is_alive():
                # « Annuler » vient d'un autre thread que celui qui lit le flux.
                cancel_thread.start()

        core.bus.subscribe(stop_on_first_chunk, types=[EventType.STREAM_CHUNK])

        started = time.monotonic()
        reply = core.run("genere beaucoup de texte")
        elapsed = time.monotonic() - started
        cancel_thread.join(timeout=5)
        llm.close()

        assert elapsed < 1.5, f"{elapsed:.2f} s : l'annulation a attendu le flux"
        assert reply.stop_reason == "cancelled", reply.to_dict()
        assert reply.error_code == "cancelled"
        assert reply.ok is False
        assert chunks, "aucun fragment reçu avant l'annulation"
        assert server.sent < 40, "le serveur a pu finir d'écrire : flux non fermé"


def test_llm_client_parses_openai_tool_calls(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    captured: list[dict] = []
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        authorizations.append(request.headers.get("authorization", ""))
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": "je regarde",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "fs_list",
                                        "arguments": '{"path": "C:/temp"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        )

    with tmpdir("llm"):
        client = _client(handler)
        try:
            response = client.complete(
                [{"role": "user", "content": "liste"}],
                tools=[{"type": "function", "function": {"name": "fs_list", "parameters": {}}}],
            )
        finally:
            client.close()

    assert response.text == "je regarde"
    assert response.tool_calls[0].name == "fs_list"
    assert response.tool_calls[0].arguments == {"path": "C:/temp"}
    assert response.usage == {"total_tokens": 42}
    assert captured[0]["tool_choice"] == "auto"
    assert "test-key" not in json.dumps(captured[0]), "la clé ne doit jamais figurer dans le corps"
    assert authorizations == ["Bearer test-key"]


def test_llm_client_parses_ollama_message(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is False
        return httpx.Response(
            200,
            json={
                "model": "llama3",
                "done": True,
                "message": {
                    "content": "",
                    "tool_calls": [{"function": {"name": "echo", "arguments": {"text": "hi"}}}],
                },
                "eval_count": 7,
            },
        )

    client = _client(handler, backend="ollama")
    try:
        response = client.complete([{"role": "user", "content": "x"}], tools=[])
    finally:
        client.close()

    assert response.tool_calls[0].arguments == {"text": "hi"}
    assert response.usage["eval_count"] == 7


def test_tool_messages_follow_the_provider_protocol(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "résultat"},
    ]

    client = _client(handler)
    try:
        client.complete(history)
    finally:
        client.close()
    tool_openai = payloads[-1]["messages"][-1]
    assert tool_openai["tool_call_id"] == "c1"
    assert "name" not in tool_openai, "champ non documenté pour un endpoint OpenAI"

    client = _client(handler, backend="ollama")
    try:
        client.complete(history)
    finally:
        client.close()
    tool_ollama = payloads[-1]["messages"][-1]
    assert tool_ollama["tool_name"] == "echo"
    assert "name" not in tool_ollama


def test_llm_client_maps_http_errors(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    client = _client(unauthorized)
    try:
        try:
            client.complete([{"role": "user", "content": "x"}])
        except LLMAuthError as exc:
            assert "TEST_LLM_KEY" in str(exc)
        else:
            raise AssertionError("401 doit lever LLMAuthError")
    finally:
        client.close()

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>pas du json</html>")

    client = _client(malformed)
    try:
        try:
            client.complete([{"role": "user", "content": "x"}])
        except LLMResponseError:
            pass
        else:
            raise AssertionError("une réponse non JSON doit lever LLMResponseError")
    finally:
        client.close()


def test_llm_client_retries_rate_limit_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")
    attempts = {"count": 0}
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="trop vite")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    client = _client(handler, max_retries=2)
    client._sleep = delays.append
    try:
        response = client.complete([{"role": "user", "content": "x"}])
    finally:
        client.close()

    assert response.text == "ok"
    assert attempts["count"] == 2
    assert delays == [1.0]
    assert retry_delay(LLMRateLimited("x", retry_after_s=3.0), attempt=1) == 3.0


def test_llm_client_streams_text(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices": [{"delta": {"content": "Bon"}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "jour"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = _client(handler)
    try:
        chunks = list(client.stream_text([{"role": "user", "content": "x"}]))
    finally:
        client.close()
    assert "".join(chunks) == "Bonjour"


def test_llm_client_streams_text_with_ollama(monkeypatch) -> None:
    requested: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        requested.append(json.loads(request.content))
        body = "\n".join(
            [
                json.dumps({"message": {"content": "Bon"}, "done": False}),
                json.dumps({"message": {"content": "jour"}, "done": False}),
                "pas du json",  # ligne parasite ignorée
                json.dumps({"message": {"content": ""}, "done": True}),
            ]
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "application/x-ndjson"}
        )

    client = _client(handler, backend="ollama")
    try:
        chunks = list(client.stream_text([{"role": "user", "content": "salut"}]))
    finally:
        client.close()

    # Les fragments vides (ligne `done`) ne sont pas émis.
    assert chunks == ["Bon", "jour"]
    assert "".join(chunks) == "Bonjour"
    assert requested[0]["stream"] is True
    assert requested[0]["model"] == "deepseek-chat"
    assert requested[0]["messages"] == [{"role": "user", "content": "salut"}]


def test_missing_api_key_is_reported(monkeypatch) -> None:
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    client = _client(lambda request: httpx.Response(200, json={}))
    try:
        try:
            client.complete([{"role": "user", "content": "x"}])
        except LLMAuthError as exc:
            assert exc.code == "llm_missing_key"
        else:
            raise AssertionError("sans clé, l'appel doit échouer avant le réseau")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Événements et prompts
# ---------------------------------------------------------------------------


def test_event_bus_is_thread_safe_and_isolates_subscribers() -> None:
    bus = EventBus(history_limit=5)
    received: list[int] = []
    unsub = bus.subscribe(lambda event: received.append(event.seq), types=[EventType.THOUGHT])

    def faulty(_event) -> None:
        raise RuntimeError("abonné cassé")

    bus.subscribe(faulty)

    def producer() -> None:
        for index in range(20):
            bus.emit(EventType.THOUGHT, {"index": index})

    threads = [threading.Thread(target=producer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(received) == 80, len(received)
    assert len(bus) == 80
    assert len(bus.events(event_type=EventType.THOUGHT)) == 5  # historique borné
    assert [event.seq for event in bus.events()] == sorted(event.seq for event in bus.events())

    unsub()
    bus.emit(EventType.THOUGHT, {})
    assert len(received) == 80

    drained = bus.poll(10)
    assert len(drained) == 10
    assert bus.wait(timeout=0.01) != []
    bus.close()
    assert bus.wait(timeout=0.05) == []


def test_prompts_describe_rules_and_tools() -> None:
    tool = FunctionTool(
        "fs_delete",
        "Supprime un fichier.",
        object_schema({}),
        lambda: None,
        read_only=False,
        dangerous=True,
    )
    prompt = system_prompt(tools=[tool], max_steps=7, language="fr")
    assert "fs_delete" in prompt and "dangereux" in prompt
    assert "7 étapes" in prompt
    assert FINAL_ANSWER_PREFIX in prompt
    assert "Agis d'abord" in prompt
    assert "[SENSIBLE]" in prompt

    catalogue = tool_catalogue([tool])
    assert catalogue.startswith("- fs_delete [dangereux]")
    assert tool_catalogue([]) == "(aucun outil disponible)"


if __name__ == "__main__":
    sys.exit(run(globals()))
