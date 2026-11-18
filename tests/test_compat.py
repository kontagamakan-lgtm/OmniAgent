"""Compatibilité de l'API historique : bus, spécifications d'outils, façade."""

from __future__ import annotations

from support import make_settings, tmpdir  # noqa: I001

from core import AgentResult, AgentRunner, run_task
from core.agent_core import AgentCore, AgentReply
from core.events import EventBus, EventType
from core.llm_client import LLMResponse
from core.tool_registry import ToolRegistry, ToolSpec
from tools.base import object_schema, param


class _OneShotLLM:
    """Fournisseur minimal : répond une fois, sans réseau."""

    def __init__(self, text: str = "terminé") -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append([dict(message) for message in messages])
        return LLMResponse(text=self.text, finish_reason="stop")


def test_history_filters_events_by_run() -> None:
    bus = EventBus(history_limit=10)
    seen: list[str] = []
    unsubscribe = bus.subscribe(
        lambda event: seen.append(event.type), types=[EventType.RUN_STARTED]
    )

    bus.emit(EventType.RUN_STARTED, {"task": "x"}, run_id="abc")
    bus.emit(EventType.STEP_STARTED, {"index": 1}, run_id="abc")

    assert seen == [EventType.RUN_STARTED]
    run_events = bus.history("abc")
    assert [event.type for event in run_events] == [
        EventType.RUN_STARTED,
        EventType.STEP_STARTED,
    ]
    assert all(event.run_id == "abc" for event in run_events)
    assert bus.history("autre") == []
    assert [event.run_id for event in bus.poll()] == ["abc", "abc"]

    unsubscribe()
    bus.emit(EventType.RUN_STARTED, {}, run_id="zzz")
    assert seen == [EventType.RUN_STARTED], "le désabonnement doit être effectif"


def test_history_returns_the_events_of_a_real_run() -> None:
    with tmpdir("compat") as tmp:
        settings = make_settings(tmp)
        bus = EventBus()
        core = AgentCore(settings, _OneShotLLM("fini"), ToolRegistry([]), bus=bus)

        first = core.run("tâche un")
        first_id = core.run_id

        assert first.ok, first.to_dict()
        assert first_id, "une exécution doit avoir un identifiant"
        run_events = bus.history(first_id)
        assert run_events, "history(run_id) doit renvoyer les événements du run"
        assert all(event.run_id == first_id for event in run_events)
        assert {event.type for event in run_events} >= {
            EventType.RUN_STARTED,
            EventType.FINAL,
            EventType.RUN_FINISHED,
        }

        second = core.run("tâche deux")
        assert second.ok
        assert core.run_id and core.run_id != first_id, "chaque exécution a son identifiant"
        assert all(event.run_id == core.run_id for event in bus.history(core.run_id))
        assert any(event.run_id == first_id for event in bus.history())


def test_tool_spec_is_accepted_by_the_registry() -> None:
    def echo(text: str) -> str:
        return text

    spec = ToolSpec(
        name="echo",
        description="Répète le texte.",
        parameters=object_schema({"text": param("string", "Texte")}, required=["text"]),
        func=echo,
    )
    registry = ToolRegistry([spec])

    assert registry.names() == ["echo"]
    assert registry.schemas()[0]["function"]["name"] == "echo"

    result = registry.dispatch("echo", {"text": "salut"})
    assert result.ok is True and result.data == "salut"
    assert registry.observation(result) == "salut"
    assert registry.observation(result, max_chars=3) == "sal\n…[tronqué : 2 caractères]"

    assert registry.dispatch("absent", {}).ok is False


def test_runner_and_run_task_return_a_result() -> None:
    with tmpdir("compat") as tmp:
        settings = make_settings(tmp)
        llm = _OneShotLLM("bonjour")

        with AgentRunner(settings, llm=llm, with_web=False, with_browser=False) as runner:
            result = runner.run("dis bonjour")

        assert AgentResult is AgentReply
        assert isinstance(result, AgentResult)
        assert result.ok and result.text == "bonjour"
        assert llm.calls, "le fournisseur doit avoir été appelé une fois"

        direct = run_task(
            "encore",
            settings=settings,
            llm=_OneShotLLM("fini"),
            with_web=False,
            with_browser=False,
        )
        assert direct.ok and direct.text == "fini"
