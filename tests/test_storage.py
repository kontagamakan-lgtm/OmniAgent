"""Stockage local : schéma, migration idempotente, événements et FTS5."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import run, tmpdir  # noqa: E402

from storage.db import (  # noqa: E402
    Database,
    SCHEMA_VERSION,
    StorageError,
    initialize_database,
    schema_path,
)


def test_database_initializes_wal_schema_and_migrations() -> None:
    with tmpdir("storage") as tmp:
        path = tmp / "omniagent.db"
        assert initialize_database(path) == path
        assert initialize_database(path) == path, "la migration doit être idempotente"

        with Database(path) as db:
            assert db.schema_version == SCHEMA_VERSION
            mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"
            tables = {
                row[0]
                for row in db.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {"runs", "events", "documents", "schema_migrations"} <= tables
            assert schema_path().is_file()


def test_runs_and_events_round_trip() -> None:
    with tmpdir("storage-runs") as tmp:
        with Database(tmp / "omniagent.db") as db:
            db.record_run("run-1", "trier les notes", tokens=12)
            db.append_event("run-1", "tool_result", {"tool": "fs_read_text", "ok": True})
            db.append_event("run-1", "run_finished", {"status": "finished"})
            db.finish_run("run-1", status="finished", tokens=42, cost_usd=0.01)

            runs = list(db.runs())
            assert runs[0]["run_id"] == "run-1"
            assert runs[0]["status"] == "finished"
            assert runs[0]["tokens"] == 42
            events = db.events("run-1")
            assert [event["kind"] for event in events] == ["tool_result", "run_finished"]
            assert events[0]["payload"]["ok"] is True


def test_documents_are_searchable_with_fts5() -> None:
    with tmpdir("storage-fts") as tmp:
        with Database(tmp / "omniagent.db") as db:
            db.index_document("notes/alpha.txt", "alpha beta gamma")
            db.index_document("notes/delta.txt", "delta epsilon")
            hits = db.search_documents("alpha")
            assert [hit["path"] for hit in hits] == ["notes/alpha.txt"]
            assert "alpha" in hits[0]["excerpt"].lower()

            db.index_document("notes/alpha.txt", "alpha beta gamma delta")
            hits = db.search_documents("delta")
            assert {hit["path"] for hit in hits} == {
                "notes/alpha.txt",
                "notes/delta.txt",
            }
            assert db.search_documents("") == []


def test_a_newer_schema_is_refused_without_writing() -> None:
    with tmpdir("storage-newer") as tmp:
        path = tmp / "omniagent.db"
        initialize_database(path)
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA user_version=99")
        connection.commit()
        connection.close()

        try:
            initialize_database(path)
        except StorageError as exc:
            assert "schéma v99" in str(exc)
        else:
            raise AssertionError("une base plus récente doit être refusée")


if __name__ == "__main__":
    sys.exit(run(globals()))
