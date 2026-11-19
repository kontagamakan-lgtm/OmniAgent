"""Transaction par run : journal, instantanés des seuls fichiers touchés, annulation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.errors import LLMAuthError  # noqa: E402
from core.factory import build_agent  # noqa: E402
from core.file_tx import (  # noqa: E402
    FileTransaction,
    transaction_dir,
    undo_run,
    undo_transaction,
)
from core.llm_client import LLMResponse, ParsedToolCall  # noqa: E402

ORIGINAL = b"version d'origine\r\nligne 2 avec accents : \xc3\xa9\r\n\x00octets bruts"


class _FailingLLM:
    """Deux écritures réelles, puis une panne non réessayable."""

    supports_stream = False

    def __init__(self, first: Path, second: Path) -> None:
        self.first = first
        self.second = second
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                text="",
                tool_calls=[
                    ParsedToolCall(
                        name="fs_write_text",
                        arguments={"path": str(self.first), "content": "remplacement"},
                        source="test",
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return LLMResponse(
                text="",
                tool_calls=[
                    ParsedToolCall(
                        name="fs_write_text",
                        arguments={"path": str(self.second), "content": "ajout"},
                        source="test",
                    )
                ],
                finish_reason="tool_calls",
            )
        raise LLMAuthError("panne simulée après deux écritures")


def _journal(settings, run_id: str) -> list[dict]:
    directory = transaction_dir(settings, run_id)
    assert directory is not None
    lines = (directory / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _failing_run(tmp: Path) -> tuple[object, Path, Path, Path, Path, bytes]:
    """Exécute un run qui écrit deux fichiers puis échoue ; retourne son contexte."""
    settings = make_settings(tmp)
    original_path = tmp / "notes.txt"
    original_path.write_bytes(ORIGINAL)
    created_path = tmp / "nouveau.txt"
    untouched_path = tmp / "hors_transaction.txt"
    untouched_path.write_bytes(b"jamais touche")
    untouched_mtime = untouched_path.stat().st_mtime_ns

    bundle = build_agent(
        settings,
        llm=_FailingLLM(original_path, created_path),
        with_web=False,
        with_browser=False,
    )
    try:
        reply = bundle.agent.run("écris deux fichiers puis échoue")
        assert not reply.ok, "le run doit échouer pour que l'annulation ait un sens"
        run_id = bundle.agent.run_id
        assert run_id
        assert original_path.read_bytes() == b"remplacement"
        assert created_path.read_bytes() == b"ajout"
    finally:
        bundle.close()

    return settings, original_path, created_path, untouched_path, run_id, untouched_mtime


def test_failed_run_undo_restores_exact_bytes_and_only_its_files() -> None:
    with tmpdir("transaction") as tmp:
        settings, notes, created, untouched, run_id, untouched_mtime = _failing_run(tmp)

        records = _journal(settings, run_id)
        intents = [record for record in records if record["type"] == "intent"]
        finished = [record for record in records if record["type"] == "run_finished"]
        assert [intent["op"] for intent in intents] == ["write", "create"]
        assert intents[0]["path"] == str(notes.resolve())
        assert intents[0]["snapshot"], "le fichier écrasé doit être copié avant mutation"
        assert intents[0]["size"] == len(ORIGINAL)
        assert intents[1]["snapshot"] == ""
        assert finished and finished[-1]["ok"] is False
        # Seul le fichier réellement touché est copié : pas l'arbre entier.
        journal_text = json.dumps(records, ensure_ascii=False)
        assert str(untouched) not in journal_text
        assert len(list((transaction_dir(settings, run_id) / "snapshots").iterdir())) == 1

        # L'aperçu ne modifie rien.
        preview = undo_run(settings, run_id, apply=False)
        assert preview is not None and preview.ok
        assert notes.read_bytes() == b"remplacement"
        assert created.exists()

        report = undo_run(settings, run_id, apply=True)
        assert report is not None and report.ok
        assert notes.read_bytes() == ORIGINAL, "les octets d'origine doivent revenir exactement"
        assert not created.exists(), "le fichier créé par le run doit disparaître"
        assert untouched.read_bytes() == b"jamais touche"
        assert untouched.stat().st_mtime_ns == untouched_mtime
        assert (transaction_dir(settings, run_id) / "undone.json").is_file()

        # Une seconde annulation est sans effet, pas destructrice.
        again = undo_run(settings, run_id, apply=True)
        assert again is not None and again.ok
        assert notes.read_bytes() == ORIGINAL


def test_move_and_delete_intents_are_reversed() -> None:
    with tmpdir("transaction-move") as tmp:
        source = tmp / "brouillon.txt"
        source.write_bytes(b"contenu a deplacer")
        doomed = tmp / "vieux.txt"
        doomed.write_bytes(b"a supprimer")

        transaction = FileTransaction(
            tmp / "transactions" / "run-manuel", run_id="run-manuel", roots=[str(tmp)]
        )
        transaction.begin()
        transaction.will_move(source, tmp / "archives" / "brouillon.txt")
        (tmp / "archives").mkdir()
        source.rename(tmp / "archives" / "brouillon.txt")
        transaction.will_delete(doomed)
        doomed.unlink()
        transaction.finish(ok=False, stop_reason="erreur")

        report = undo_transaction(tmp / "transactions" / "run-manuel", apply=True, roots=[str(tmp)])
        assert report.ok
        assert source.read_bytes() == b"contenu a deplacer"
        assert not (tmp / "archives" / "brouillon.txt").exists()
        assert doomed.read_bytes() == b"a supprimer"


def test_undo_refuses_a_path_outside_the_recorded_roots() -> None:
    with tmpdir("transaction-garde") as tmp:
        outside = tmp.parent / f"{tmp.name}-exterieur.txt"
        outside.write_bytes(b"hors zone")
        try:
            directory = tmp / "transactions" / "run-forge"
            transaction = FileTransaction(directory, run_id="run-forge", roots=[str(tmp)])
            transaction.begin()
            with (directory / "journal.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "intent",
                            "seq": 1,
                            "op": "create",
                            "path": str(outside),
                            "destination": "",
                            "kind": "file",
                            "existed": False,
                            "snapshot": "",
                            "size": 0,
                            "mtime_ns": 0,
                        }
                    )
                    + "\n"
                )
            report = undo_transaction(directory, apply=True, roots=[str(tmp)])
            assert not report.ok
            assert report.steps[0].status.startswith("refuse")
            assert outside.read_bytes() == b"hors zone"
        finally:
            outside.unlink(missing_ok=True)


def test_snapshot_budget_is_reported_instead_of_silently_skipped() -> None:
    with tmpdir("transaction-plafond") as tmp:
        target = tmp / "gros.txt"
        target.write_bytes(b"x" * 64)
        transaction = FileTransaction(
            tmp / "transactions" / "run-plafond",
            run_id="run-plafond",
            roots=[str(tmp)],
            max_snapshot_bytes=8,
        )
        transaction.begin()
        transaction.will_write(target)
        transaction.finish(ok=True)

        report = undo_transaction(
            tmp / "transactions" / "run-plafond", apply=False, roots=[str(tmp)]
        )
        assert not report.ok
        assert report.steps[0].status.startswith("impossible")
        assert target.read_bytes() == b"x" * 64


def test_cli_exposes_the_undo_command() -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "core.file_tx", "--help"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--undo" in result.stdout and "--list" in result.stdout


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
