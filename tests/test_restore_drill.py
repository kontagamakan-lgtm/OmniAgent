"""Répétition de restauration : archive saine acceptée, archive cassée refusée."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import app  # noqa: E402
from core.backup import create_backup  # noqa: E402
from core.restore_drill import drill_archive  # noqa: E402


def _live_data(tmp: Path) -> dict[str, tuple[bytes, int]]:
    """Octets et mtime des fichiers vivants, avant le drill."""
    state: dict[str, tuple[bytes, int]] = {}
    for path in tmp.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".ombak") or ".restore-drill-" in path.name:
            continue
        state[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return state


def _healthy_backup(tmp: Path) -> tuple[object, Path]:
    settings = make_settings(tmp)
    (tmp / "config.json").write_text(json.dumps({"llm": {"model": "test"}}), encoding="utf-8")
    connection = sqlite3.connect(tmp / "omniagent.db")
    try:
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO runs VALUES ('run-1')")
        connection.commit()
    finally:
        connection.close()
    runs = tmp / "logs" / "runs"
    runs.mkdir(parents=True)
    (runs / "run-1.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"run_id": "run-1", "seq": 1, "type": "run_started", "payload": {}},
                {"run_id": "run-1", "seq": 2, "type": "run_finished", "payload": {"ok": True}},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    autonomy = tmp / "autonomy"
    autonomy.mkdir()
    (autonomy / "journal.jsonl").write_text(
        json.dumps({"type": "cycle_finished", "ok": True}) + "\n", encoding="utf-8"
    )
    archive = create_backup(settings, tmp / "backup.ombak")
    return settings, archive


def test_healthy_backup_drill_leaves_live_data_untouched() -> None:
    with tmpdir("drill-sain") as tmp:
        settings, archive = _healthy_backup(tmp)
        before = _live_data(tmp)
        report = drill_archive(settings, archive)

        assert report.ok, report.format()
        assert report.files >= 4
        assert report.cleaned
        assert not list(tmp.glob(".restore-drill-*"))
        after = _live_data(tmp)
        assert after == before, "le drill doit seulement lire les données vivantes"
        names = " ".join(name for name, _ok, _detail in report.checks)
        assert "trace" in names and "base" in names and "index" not in names


def test_corrupted_archive_is_refused_before_any_write() -> None:
    with tmpdir("drill-casse") as tmp:
        settings, archive = _healthy_backup(tmp)
        corrupted = tmp / "corrompu.ombak"
        blob = archive.read_bytes()
        corrupted.write_bytes(blob[: len(blob) // 2])

        report = drill_archive(settings, corrupted)
        assert not report.ok
        assert not report.cleaned
        assert report.files == 0
        assert not list(tmp.glob(".restore-drill-*")), "aucun dossier temporaire ne doit rester"


def test_cli_exposes_restore_drill_and_undo_run() -> None:
    parser = app.build_parser()
    undo = parser.parse_args(["--undo-run", "run-1"])
    assert undo.undo_run == "run-1" and undo.undo_apply is False
    drill = parser.parse_args(["--restore-drill", "backup.ombak"])
    assert drill.restore_drill == "backup.ombak"


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
