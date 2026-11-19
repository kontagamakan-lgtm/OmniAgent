"""Paquet de support : diagnostic partageable, sans secret ni contenu de session."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

from core.support_bundle import build_support_bundle, bundle_summary
from core.trace import RunTrace
from core.events import Event, EventType


def _finished_run(directory: Path, run_id: str = "run-a") -> None:
    trace = RunTrace(directory)
    trace(
        Event(
            type=EventType.RUN_STARTED,
            run_id=run_id,
            payload={"task": "secret de session", "model": "m", "trace_format": 3},
            seq=1,
        )
    )
    trace(
        Event(
            type=EventType.RUN_FINISHED,
            run_id=run_id,
            payload={"ok": True, "stop_reason": "final"},
            seq=2,
        )
    )


def test_the_bundle_contains_facts_and_no_secret(monkeypatch) -> None:
    with tmpdir("support") as tmp:
        settings = make_settings(tmp)
        monkeypatch.setenv(settings.llm.api_key_env, "sk-test-secret-123")
        _finished_run(settings.paths.log_dir / "runs")
        target = Path(tmp) / "support.zip"

        path = build_support_bundle(settings, target)

        assert path == target and path.is_file()
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            assert {"README.txt", "summary.json", "index-doctor.txt", "retention.txt"} <= names
            blob = b"".join(archive.read(name) for name in names)
            summary = json.loads(archive.read("summary.json").decode("utf-8"))

        assert b"sk-test-secret-123" not in blob, "aucune clé ne doit fuiter"
        assert b"secret de session" not in blob, "aucune tâche ne doit fuiter"
        assert b"run_started" not in blob, "aucune trace n'est copiée"
        assert summary["app"]["version"]
        assert summary["index"][0]["run_id"] == "run-a"


def test_a_summary_survives_a_missing_runs_directory() -> None:
    with tmpdir("support-vide") as tmp:
        settings = make_settings(tmp)

        summary = bundle_summary(settings)
        path = build_support_bundle(settings, Path(tmp) / "vide.zip")

        assert summary["index"] == [] and summary["index_files"] == 0
        assert path.is_file()


def test_secret_looking_fields_are_masked() -> None:
    from core.support_bundle import _redact_tree

    masked = _redact_tree(
        {"api_key": "sk-valeur", "nested": {"passphrase": "phrase"}, "model": "m"}
    )

    assert masked == {"api_key": "***", "nested": {"passphrase": "***"}, "model": "m"}
