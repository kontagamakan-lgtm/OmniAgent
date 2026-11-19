"""Métriques par run : un enregistrement à la fin, un agrégat lisible."""

from __future__ import annotations

import json
from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

from core.events import EventBus, EventType
from core.metrics import (
    MetricsRecorder,
    build_metrics,
    format_summary,
    metrics_path,
    summarize,
)


def _run(bus: EventBus, *, ok: bool = True, run_id: str = "r") -> None:
    bus.emit(EventType.RUN_STARTED, {"model": "m", "workspace": "w"}, run_id=run_id)
    bus.emit(EventType.STEP_STARTED, {"step": 1}, run_id=run_id)
    bus.emit(
        EventType.LLM_RESPONSE,
        {"tokens": {"prompt": 100, "completion": 20}},
        run_id=run_id,
    )
    bus.emit(EventType.TOOL_STARTED, {"tool": "shell_run"}, run_id=run_id)
    bus.emit(
        EventType.TOOL_FINISHED,
        {"tool": "shell_run", "ok": False, "error_code": "denied"},
        run_id=run_id,
    )
    bus.emit(
        EventType.CONTEXT_TRIMMED,
        {"tokens_before": 500, "tokens_after": 300, "dropped": 2, "kept": 8},
        run_id=run_id,
    )
    bus.emit(
        EventType.RUN_FINISHED,
        {
            "ok": ok,
            "stop_reason": "final" if ok else "error",
            "tool_calls": 1,
            "summary": {
                "duration_s": 1.5,
                "steps": 1,
                "tool_calls": 1,
                "requests": 1,
                "prompt_tokens": 120,
                "completion_tokens": 20,
                "trims": 1,
                "trimmed_tokens": 200,
            },
        },
        run_id=run_id,
    )


def test_one_record_per_finished_run_aggregates_tool_errors() -> None:
    with tmpdir("metriques") as tmp:
        path = Path(tmp) / "metrics.jsonl"
        recorder = MetricsRecorder(path)
        bus = EventBus()
        bus.subscribe(recorder)

        _run(bus, ok=True, run_id="a")
        _run(bus, ok=False, run_id="b")

        records = recorder.all()
        assert [record["run_id"] for record in records] == ["a", "b"]
        assert records[0]["tool_errors"] == {"shell_run": 1}
        assert records[0]["prompt_tokens"] == 120
        assert records[0]["trims"] == 1

        data = summarize(records)
        assert data["runs"] == 2 and data["failed"] == 1
        assert data["tool_errors"]["shell_run"] == 2
        assert "shell_run:2" in format_summary(records)
        assert "2 (1 ok, 1 echec" in format_summary(records)


def test_the_metrics_file_is_bounded() -> None:
    with tmpdir("metriques-borne") as tmp:
        path = Path(tmp) / "metrics.jsonl"
        recorder = MetricsRecorder(path, max_entries=2)
        bus = EventBus()
        bus.subscribe(recorder)

        for number in range(4):
            _run(bus, run_id=str(number))

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[-1])["run_id"] == "3"
        assert format_summary([]) == "Aucune métrique enregistrée."


def test_metrics_follow_the_configuration() -> None:
    with tmpdir("metriques-config") as tmp:
        settings = make_settings(tmp)
        recorder = build_metrics(settings)
        assert recorder is not None and recorder.path == metrics_path(settings)

        settings.metrics.enabled = False
        assert build_metrics(settings) is None
