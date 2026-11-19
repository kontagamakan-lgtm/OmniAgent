"""Vérificateur de format des traces : chaque invariant a son test.

Le lecteur tolère ; le vérificateur juge. Ces tests fixent le contrat du format
2 pour qu'une évolution de schéma casse ici plutôt qu'en production.
"""

from __future__ import annotations

from pathlib import Path

from support import tmpdir  # noqa: I001

from core.events import Event, EventType
from core.trace import RunTrace
from core.trace_lint import lint_events, lint_file


def _record(event_type: str, seq: int, payload: dict | None = None, run_id: str = "r"):
    return {
        "run_id": run_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "type": event_type,
        "seq": seq,
        "payload": payload or {},
    }


def _valid() -> list[dict]:
    return [
        _record("run_started", 1, {"trace_format": 2, "app_version": "1.0.0"}),
        _record("step_started", 2, {"index": 1}),
        _record("tool_started", 3, {"step": 1, "tool": "fs_list"}),
        _record("tool_finished", 4, {"step": 1, "tool": "fs_list", "ok": True}),
        _record("final", 5, {"step": 2}),
        _record("run_finished", 6, {"stop_reason": "final", "ok": True}),
    ]


def _codes(records: list[dict]) -> set[str]:
    return {issue.code for issue in lint_events(records)}


def test_a_well_formed_trace_has_no_issue() -> None:
    issues = lint_events(_valid())
    assert issues == [], [str(issue) for issue in issues]


def test_duplicate_run_started_is_an_error() -> None:
    records = _valid()
    records.insert(1, _record("run_started", 2, {"trace_format": 2}))
    assert "duplicate_run_started" in _codes(records)


def test_run_started_must_open_the_trace() -> None:
    records = _valid()
    records.insert(0, _record("thought", 0, {"text": "avant"}))
    assert "run_started_not_first" in _codes(records)


def test_missing_run_finished_is_an_interruption() -> None:
    issues = lint_events(_valid()[:-1])
    assert any(issue.code == "missing_run_finished" and issue.error for issue in issues)


def test_a_truncated_trace_may_leave_a_tool_call_open() -> None:
    records = _valid()[:3] + [_record("trace_truncated", 9, {"max_bytes": 10})]
    issues = lint_events(records)
    assert not any(issue.code == "missing_run_finished" for issue in issues)
    assert any(
        issue.code == "tool_started_without_finish" and not issue.error for issue in issues
    )


def test_an_orphan_tool_finished_is_an_error() -> None:
    records = [record for record in _valid() if record["type"] != "tool_started"]
    issues = lint_events(records)
    assert any(issue.code == "tool_finished_without_start" and issue.error for issue in issues)


def test_sequence_numbers_must_increase() -> None:
    records = _valid()
    records[2]["seq"] = 2
    assert "seq_not_increasing" in _codes(records)


def test_events_after_run_finished_are_reported() -> None:
    records = _valid() + [_record("thought", 7, {"text": "après"})]
    assert "events_after_run_finished" in _codes(records)


def test_mixed_run_ids_are_reported() -> None:
    records = _valid()
    records[3]["run_id"] = "autre"
    assert "mixed_run_ids" in _codes(records)


def test_a_newer_format_is_refused() -> None:
    records = _valid()
    records[0]["payload"]["trace_format"] = 99
    assert "newer_trace_format" in _codes(records)


def test_unknown_event_type_is_only_a_warning() -> None:
    records = _valid()
    records.insert(2, _record("inventé", 2))
    issues = lint_events(records)
    assert any(issue.code == "unknown_event_type" and not issue.error for issue in issues)


def test_writer_output_passes_the_lint() -> None:
    """La trace réellement produite doit satisfaire le contrat qu'on publie."""
    with tmpdir("lint") as tmp:
        trace = RunTrace(Path(tmp) / "runs")
        events = [
            (EventType.RUN_STARTED, {"task": "x", "trace_format": 2}),
            (EventType.STEP_STARTED, {"index": 1}),
            (EventType.TOOL_STARTED, {"step": 1, "tool": "fs_list", "arguments": {}}),
            (EventType.TOOL_FINISHED, {"step": 1, "tool": "fs_list", "ok": True}),
            (EventType.FINAL, {"step": 2, "text": "fini"}),
            (EventType.RUN_FINISHED, {"stop_reason": "final", "ok": True}),
        ]
        for seq, (event_type, payload) in enumerate(events, start=1):
            trace(Event(type=event_type, payload=payload, run_id="ok", seq=seq))
        assert trace.path is not None

        issues, report = lint_file(trace.path)

        assert report.ignored == 0
        assert [issue for issue in issues if issue.error] == []


def test_a_truncated_written_trace_passes_with_a_warning() -> None:
    with tmpdir("lint-coupe") as tmp:
        trace = RunTrace(Path(tmp) / "runs", max_age_days=0, max_bytes=2_000)
        trace(Event(type=EventType.RUN_STARTED, run_id="coupe", payload={"trace_format": 2}, seq=1))
        for seq in range(2, 40):
            trace(
                Event(
                    type=EventType.STREAM_CHUNK,
                    run_id="coupe",
                    payload={"step": 1, "text": "z" * 120},
                    seq=seq,
                )
            )
        trace.close()
        assert trace.path is not None

        issues, _ = lint_file(trace.path)

        assert [issue.code for issue in issues if issue.error] == []
        assert any(issue.code == "missing_run_finished" for issue in issues) is False


def test_unreadable_lines_are_errors_unless_they_are_the_final_cut() -> None:
    with tmpdir("lint-illisible") as tmp:
        path = Path(tmp) / "run.jsonl"
        path.write_text(
            '{"run_id": "r", "timestamp": "t", "type": "run_started", "seq": 1, "payload": {"trace_format": 2}}\n'
            "{ceci n'est pas du json}\n"
            '{"run_id": "r", "timestamp": "t", "type": "run_finished", "seq": 2, "payload": {}}\n',
            encoding="utf-8",
        )

        issues, report = lint_file(path)

        assert report.ignored == 1
        assert any(issue.code == "unreadable_lines" and issue.error for issue in issues)

        cut = Path(tmp) / "coupe.jsonl"
        cut.write_text(
            '{"run_id": "r", "timestamp": "t", "type": "run_started", "seq": 1, "payload": {"trace_format": 2}}\n'
            '{"run_id": "r", "timestamp": "t", "type": "run_finished", "seq": 2',
            encoding="utf-8",
        )
        issues, report = lint_file(cut)
        assert report.truncated_final is True
        assert not any(issue.code == "unreadable_lines" for issue in issues)
