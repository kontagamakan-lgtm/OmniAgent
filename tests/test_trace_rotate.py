"""Rotation de la clé des traces : réécriture atomique, .gz, reprise, index."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from support import tmpdir  # noqa: I001

from core.events import Event, EventType
from core.replay import main
from core.run_index import RunIndex
from core.trace import RunTrace, TraceKeyError, read_events
from core.trace_crypto import decrypt_record
from core.trace_rotate import rotate_traces

OLD = bytes(range(32))
NEW = bytes(reversed(range(32)))


def _b64(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii")


def _write_trace(
    directory: Path,
    run_id: str,
    task: str,
    key: bytes,
    *,
    archive: bool = False,
) -> Path:
    trace = RunTrace(
        directory,
        encrypt_key=key,
        archive=archive,
        archive_min_bytes=1 if archive else 0,
    )
    trace(
        Event(
            type=EventType.RUN_STARTED,
            run_id=run_id,
            payload={"task": task, "model": "m", "trace_format": 3},
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
    assert trace.path is not None
    return trace.path


def test_rotation_rekeys_a_trace_and_refreshes_the_index() -> None:
    with tmpdir("rotation") as tmp:
        directory = Path(tmp) / "runs"
        path = _write_trace(directory, "run", "tache-secrete", OLD)
        before = RunIndex(directory).entries()["run"]
        assert before.state == "finished" and before.model == "m"

        report = rotate_traces(directory, OLD, NEW)

        assert [(Path(item["path"]).name, item["status"]) for item in report] == [
            ("run.jsonl", "rotated")
        ]
        assert "tache-secrete" not in path.read_text(encoding="utf-8")
        with pytest.raises(TraceKeyError):
            list(read_events(path, key=OLD))
        records = list(read_events(path, key=NEW))
        assert records[0]["payload"]["task"] == "tache-secrete"

        entry = RunIndex(directory).entries()["run"]
        assert entry.state == "finished" and entry.model == "m"
        assert entry.path == str(path)
        assert entry.bytes == path.stat().st_size, "la taille de l'index doit suivre"
        assert not list(directory.glob("*.tmp")), "aucun temporaire ne doit rester"


def test_rotation_handles_an_archived_gzip_trace() -> None:
    with tmpdir("rotation-gz") as tmp:
        directory = Path(tmp) / "runs"
        path = _write_trace(directory, "archive", "contenu-gz", OLD, archive=True)
        assert path.name.endswith(".jsonl.gz") and path.exists()

        report = rotate_traces(directory, OLD, NEW)

        assert [item["status"] for item in report] == ["rotated"]
        assert path.exists() and path.name.endswith(".jsonl.gz")
        assert path.read_bytes()[:2] == b"\x1f\x8b", "le fichier doit rester du gzip"
        with pytest.raises(TraceKeyError):
            list(read_events(path, key=OLD))
        records = list(read_events(path, key=NEW))
        assert records[0]["payload"]["task"] == "contenu-gz"
        assert records[-1]["type"] == EventType.RUN_FINISHED


def test_a_failing_file_keeps_its_old_key_and_no_run_is_lost() -> None:
    with tmpdir("rotation-echec") as tmp:
        directory = Path(tmp) / "runs"
        good = _write_trace(directory, "bon", "tache-bonne", OLD)
        bad = _write_trace(directory, "abime", "tache-abimee", OLD)

        # Chiffré valide mais altéré : ni l'ancienne ni la nouvelle clé n'ouvrent.
        lines = bad.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[-1])
        ciphertext = record["enc"]["ct"]
        record["enc"]["ct"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        lines[-1] = json.dumps(record, ensure_ascii=False)
        bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
        intact = bad.read_bytes()

        report = rotate_traces(directory, OLD, NEW)

        statuses = {Path(item["path"]).name: item["status"] for item in report}
        assert statuses == {"bon.jsonl": "rotated", "abime.jsonl": "failed"}
        assert bad.read_bytes() == intact, "le fichier en échec reste intact"
        first = json.loads(bad.read_text(encoding="utf-8").splitlines()[0])
        assert decrypt_record(first, OLD)["payload"]["task"] == "tache-abimee", (
            "le fichier en échec reste sous l'ancienne clé"
        )
        assert list(read_events(good, key=NEW)), "le run sain a bien été tourné"
        assert len(list(directory.glob("*.jsonl"))) == 2, "aucun run perdu"
        assert not list(directory.glob("*.rotate.tmp"))


def test_rotation_is_resumable_and_reports_already_rotated(monkeypatch, capsys) -> None:
    with tmpdir("rotation-reprise") as tmp:
        directory = Path(tmp) / "runs"
        done = _write_trace(directory, "deja", "deja-tournee", NEW)
        pending = _write_trace(directory, "restant", "a-tourner", OLD)
        monkeypatch.setenv("OMNI_TEST_OLD", _b64(OLD))
        monkeypatch.setenv("OMNI_TEST_NEW", _b64(NEW))

        code = main(
            [
                "--rotate-key",
                str(directory),
                "--key-env",
                "OMNI_TEST_OLD",
                "--new-key-env",
                "OMNI_TEST_NEW",
            ]
        )

        output = capsys.readouterr().out.splitlines()
        assert code == 0
        assert any(line.startswith("rotated") and pending.name in line for line in output)
        assert any(line.startswith("already") and done.name in line for line in output)
        assert list(read_events(pending, key=NEW))[0]["payload"]["task"] == "a-tourner"


def test_rotation_without_the_old_key_changes_nothing(monkeypatch) -> None:
    with tmpdir("rotation-sans-cle") as tmp:
        directory = Path(tmp) / "runs"
        path = _write_trace(directory, "intact", "contenu", OLD)
        intact = path.read_bytes()
        monkeypatch.delenv("OMNI_TEST_MISSING", raising=False)
        monkeypatch.setenv("OMNI_TEST_NEW", _b64(NEW))

        code = main(
            [
                "--rotate-key",
                str(directory),
                "--key-env",
                "OMNI_TEST_MISSING",
                "--new-key-env",
                "OMNI_TEST_NEW",
            ]
        )

        assert code == 2
        assert path.read_bytes() == intact, "aucune écriture sans ancienne clé"
