"""Rejeu hors-ligne d'une trace déjà écrite."""

from __future__ import annotations

from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

from core.agent_core import AgentCore
from core.events import Event, EventBus, EventType
from core.llm_client import LLMResponse, ParsedToolCall
from core.replay import format_run, replay
from core.tool_registry import ToolRegistry
from core.trace import RunTrace, TraceFormatError, read_events, read_trace
from tools.base import FunctionTool, object_schema, param


class _ScriptedLLM:
    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)

    def complete(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)


def _ping_tool() -> FunctionTool:
    def ping(text: str = "") -> str:
        return f"pong {text}"

    return FunctionTool(
        "ping",
        "Répond pong.",
        object_schema({"text": param("string", "Texte", default="")}),
        ping,
        read_only=True,
    )


def _record_a_run(tmp: Path) -> tuple[str, Path]:
    """Écrit une vraie trace : un appel d'outil, puis une réponse finale."""
    settings = make_settings(tmp)
    bus = EventBus()
    trace = RunTrace(settings.paths.log_dir / "runs")
    unsubscribe = bus.subscribe(trace)
    core = AgentCore(
        settings,
        _ScriptedLLM(
            LLMResponse(
                text="",
                tool_calls=[ParsedToolCall(name="ping", arguments={"text": "a"}, source="test")],
                finish_reason="tool_calls",
            ),
            LLMResponse(text="terminé", finish_reason="stop"),
        ),
        ToolRegistry([_ping_tool()]),
        bus=bus,
    )
    try:
        assert core.run("vérifie la trace").ok
    finally:
        unsubscribe()
        trace.close()
    assert trace.path is not None
    return core.run_id, trace.path


def test_replay_rebuilds_a_run_from_its_trace() -> None:
    with tmpdir("rejeu") as tmp:
        run_id, path = _record_a_run(tmp)

        run = replay(path)

        assert run.run_id == run_id
        assert run.task == "vérifie la trace"
        assert run.app_version, "le manifeste doit être relu"
        assert run.model
        assert run.trace_format >= 2
        assert len(run.settings_digest) == 16
        assert run.tools == ["ping"]
        assert run.final == "terminé"
        assert run.stop_reason == "final" and run.ok is True
        assert run.truncated is False
        assert run.report.ignored == 0
        assert [step.index for step in run.steps] == [1, 2]

        text = format_run(run)
        assert "Etape 1" in text and "ping" in text
        assert "terminé" in text
        assert "evenement(s) lus" in text


def test_replay_reports_a_truncated_trace() -> None:
    with tmpdir("coupe") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, max_age_days=0, max_bytes=4_000)
        trace(Event(type=EventType.RUN_STARTED, run_id="coupe", payload={"task": "t"}))
        for _ in range(60):
            trace(
                Event(
                    type=EventType.STREAM_CHUNK,
                    run_id="coupe",
                    payload={"text": "z" * 120},
                )
            )
        trace.close()
        assert trace.path is not None

        run = replay(trace.path)

        assert run.truncated is True
        assert "TRONQUEE" in format_run(run)


def test_strict_reading_refuses_a_corrupted_line() -> None:
    with tmpdir("strict") as tmp:
        _, path = _record_a_run(tmp)
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(
            lines[0] + "\n{ceci n'est pas du json}\n" + "\n".join(lines[1:]) + "\n",
            encoding="utf-8",
        )

        records, report = read_trace(path)

        assert report.ignored == 1
        assert report.events == len(lines)
        assert report.truncated_final is False
        assert len(records) == len(lines)
        try:
            list(read_events(path, strict=True))
        except TraceFormatError:
            pass
        else:
            raise AssertionError("le mode strict doit refuser la ligne corrompue")
