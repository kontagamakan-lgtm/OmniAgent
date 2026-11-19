"""Chiffrement des traces au repos : clé, opacité, intégrité, fail-closed."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from support import make_settings, tmpdir  # noqa: I001

import config
from core.events import Event, EventType
from core.replay import replay
from core.run_index import INDEX_NAME, RunIndex
from core.trace import RunTrace, TraceKeyError, build_trace, read_events
from core.trace_crypto import (
    KEY_ENV,
    decrypt_record,
    encrypt_record,
    load_key,
    optional_key,
)
from core.trace_lint import lint_file

#: Clé fixe de test : le contenu importe peu, seule la longueur compte.
KEY = bytes(range(32))
WRONG = bytes(reversed(range(32)))


def _started(run_id: str, **payload: object) -> Event:
    body = {"trace_format": config.TRACE_FORMAT_VERSION, **payload}
    return Event(type=EventType.RUN_STARTED, run_id=run_id, payload=body, seq=1)


def test_a_key_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(KEY_ENV, base64.urlsafe_b64encode(KEY).decode())
    assert load_key() == KEY
    assert optional_key() == KEY

    monkeypatch.setenv(KEY_ENV, KEY.hex())
    assert load_key() == KEY

    monkeypatch.setenv(KEY_ENV, "trop-court")
    with pytest.raises(TraceKeyError):
        load_key()

    monkeypatch.delenv(KEY_ENV)
    assert optional_key() is None, "absence de variable n'est pas une erreur de lecture"
    with pytest.raises(TraceKeyError):
        load_key()


def test_an_encrypted_trace_hides_the_content_and_replays_with_the_key() -> None:
    with tmpdir("chiffre") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, encrypt_key=KEY)
        trace(
            _started(
                "chiffre",
                task="tâche confidentielle",
                app_version="1.2",
                model="m",
            )
        )
        trace(
            Event(
                type=EventType.LLM_RESPONSE,
                run_id="chiffre",
                payload={"step": 0, "text": "réponse secrète"},
                seq=2,
            )
        )
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="chiffre",
                payload={"ok": True, "stop_reason": "final"},
                seq=3,
            )
        )

        path = directory / "chiffre.jsonl"
        raw = path.read_text(encoding="utf-8")
        assert "confidentielle" not in raw and "secrète" not in raw
        assert '"type": "run_finished"' in raw, (
            "le type doit rester lisible : rétention et détection d'interruption "
            "travaillent sans la clé"
        )
        first = json.loads(raw.splitlines()[0])
        assert "payload" not in first and "enc" in first
        assert first["meta"]["model"] == "m"
        assert first["meta"]["trace_format"] == config.TRACE_FORMAT_VERSION

        with pytest.raises(TraceKeyError):
            list(read_events(path))
        with pytest.raises(TraceKeyError):
            list(read_events(path, key=WRONG))

        records = list(read_events(path, key=KEY))
        assert records[0]["payload"]["task"] == "tâche confidentielle"
        assert "enc" not in records[0] and "meta" not in records[0], (
            "le lecteur rend la forme d'une trace en clair"
        )

        issues, _report = lint_file(path, key=KEY)
        assert [issue for issue in issues if issue.error] == []

        run = replay(path, key=KEY)
        assert run.task == "tâche confidentielle"
        assert run.ok is True and run.interrupted is False


def test_the_truncation_marker_is_encrypted_too() -> None:
    with tmpdir("chiffre-tronque") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, max_bytes=1200, encrypt_key=KEY)
        trace(_started("coupe", task="x" * 400))
        for number in range(2, 40):
            trace(
                Event(
                    type=EventType.THOUGHT,
                    run_id="coupe",
                    payload={"text": "y" * 200},
                    seq=number,
                )
            )
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="coupe",
                payload={"ok": True},
                seq=99,
            )
        )

        assert trace.truncated is True
        raw = (directory / "coupe.jsonl").read_text(encoding="utf-8")
        assert all(json.loads(line).get("enc") for line in raw.splitlines()), (
            "aucune ligne en clair dans une trace chiffrée"
        )

        records = list(read_events(trace.path, key=KEY))
        assert records[-1]["type"] == EventType.TRACE_TRUNCATED
        assert records[-1]["payload"]["max_bytes"] == 1200


def test_a_tampered_or_moved_line_is_refused() -> None:
    record = {"run_id": "r", "type": "final", "seq": 2, "payload": {"text": "x"}}
    sealed = encrypt_record(record, KEY)
    assert decrypt_record(sealed, KEY)["payload"] == {"text": "x"}

    moved = dict(sealed, seq=3)
    with pytest.raises(TraceKeyError):
        decrypt_record(moved, KEY)

    altered = json.loads(json.dumps(sealed))
    ciphertext = altered["enc"]["ct"]
    altered["enc"]["ct"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    with pytest.raises(TraceKeyError):
        decrypt_record(altered, KEY)

    with pytest.raises(TraceKeyError):
        decrypt_record(sealed, WRONG)


def test_missing_key_disables_tracing_instead_of_writing_plaintext(monkeypatch) -> None:
    with tmpdir("cle-absente") as tmp:
        settings = make_settings(tmp)
        settings.trace.encrypt = True
        monkeypatch.delenv(settings.trace.key_env, raising=False)

        assert build_trace(settings) is None, "pas de repli en clair"
        assert not (settings.paths.log_dir / "runs").exists()

        monkeypatch.setenv(
            settings.trace.key_env, base64.urlsafe_b64encode(KEY).decode()
        )
        trace = build_trace(settings)
        assert trace is not None and trace.encrypt_key == KEY
        trace(_started("cle", task="secret-cle"))
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="cle",
                payload={"ok": True},
                seq=2,
            )
        )
        raw = (trace.directory / "cle.jsonl").read_text(encoding="utf-8")
        assert "secret-cle" not in raw and '"enc"' in raw


def test_validation_warns_when_the_encryption_key_is_missing(monkeypatch) -> None:
    settings = config.Settings()
    settings.trace.encrypt = True
    monkeypatch.delenv(settings.trace.key_env, raising=False)

    fields = {issue.field for issue in settings.validate() if issue.level == "warning"}
    assert "trace.key_env" in fields

    monkeypatch.setenv(settings.trace.key_env, base64.urlsafe_b64encode(KEY).decode())
    fields = {issue.field for issue in settings.validate() if issue.level == "warning"}
    assert "trace.key_env" not in fields


def test_the_index_rebuilds_manifest_metadata_without_the_key() -> None:
    with tmpdir("index-chiffre") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, encrypt_key=KEY)
        trace(_started("idx", model="m", workspace="w"))
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="idx",
                payload={"ok": True},
                seq=2,
            )
        )
        (directory / INDEX_NAME).unlink(missing_ok=True)

        rebuilt = RunIndex(directory).rebuild()

        assert rebuilt["idx"].model == "m"
        assert rebuilt["idx"].workspace == "w"


def test_interruption_is_still_detected_on_encrypted_traces() -> None:
    with tmpdir("chiffre-interrompu") as tmp:
        directory = Path(tmp) / "runs"
        dead = RunTrace(directory, encrypt_key=KEY)
        dead(_started("mort"))
        dead.close()  # aucun run_finished : processus tué

        alive = RunTrace(directory, encrypt_key=KEY)
        alive(_started("vivant"))
        alive(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="vivant",
                payload={},
                seq=2,
            )
        )

        entries = RunIndex(directory).entries()

        assert entries["mort"].state == "interrupted"
        assert entries["vivant"].state == "finished"
