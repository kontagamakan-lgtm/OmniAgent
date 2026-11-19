"""Diagnostic index/traces : expliquer les écarts au lieu de reconstruire à l'aveugle."""

from __future__ import annotations

from pathlib import Path

from support import tmpdir  # noqa: I001

from core.events import Event, EventType
from core.index_doctor import diagnose, format_issues
from core.replay import main
from core.trace import RunTrace
from trace_corpus import build_corpus


def test_a_clean_run_reports_nothing() -> None:
    with tmpdir("docteur-sain") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory)
        trace(Event(type=EventType.RUN_STARTED, run_id="propre", payload={"model": "m"}, seq=1))
        trace(Event(type=EventType.RUN_FINISHED, run_id="propre", payload={"ok": True}, seq=2))

        issues = diagnose(directory)

        assert issues == [], format_issues(issues)


def test_a_missing_directory_is_reported_without_error() -> None:
    with tmpdir("docteur-absent") as tmp:
        issues = diagnose(Path(tmp) / "absent")

        assert [issue.code for issue in issues] == ["missing_directory"]
        assert not issues[0].error, "un dossier pas encore créé n'est pas une panne"


def test_the_corrupted_corpus_is_explained() -> None:
    with tmpdir("docteur-corpus") as tmp:
        root = Path(tmp)
        build_corpus(root)
        # Trace déposée après coup, donc jamais indexée.
        (root / "runs" / "tardive.jsonl").write_text(
            '{"run_id": "tardive", "type": "run_started", "seq": 1, "payload": {}}\n',
            encoding="utf-8",
        )

        issues = diagnose(root / "runs", active_ttl_s=0)
        codes = {issue.code for issue in issues}

        assert {
            "index_unreadable",
            "index_duplicate",
            "index_orphan",
            "trace_not_indexed",
            "active_stale",
        } <= codes, codes
        assert any(issue.error for issue in issues), "une ligne d'index illisible est une erreur"
        assert "ERREUR" in format_issues(issues)


def test_the_cli_fails_on_an_unreadable_index(capsys) -> None:
    with tmpdir("docteur-cli") as tmp:
        root = Path(tmp)
        build_corpus(root)

        code = main(["--index-doctor", str(root / "runs")])

        output = capsys.readouterr().out
        assert code == 1
        assert "index_unreadable" in output
        assert "ERREUR" in output
