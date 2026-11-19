"""Journal d'audit chaîné : modification, suppression et réordre détectés."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.audit import (  # noqa: E402
    _entry_hash,
    audit_path,
    export_anchor,
    record_policy_decision,
    seal_all,
    seal_trace,
    verify,
    verify_anchor,
    verify_trace,
)


def _write_trace(tmp: Path, name: str = "run-a.jsonl") -> Path:
    runs = tmp / "logs" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / name
    records = [
        {"run_id": "run-a", "seq": 1, "type": "run_started", "payload": {"task": "test"}},
        {"run_id": "run-a", "seq": 2, "type": "step_started", "payload": {"index": 1}},
        {"run_id": "run-a", "seq": 3, "type": "run_finished", "payload": {"ok": True}},
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    return path


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_sealed_trace_verifies_clean() -> None:
    with tmpdir("audit-sain") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        entry = seal_trace(settings, trace)
        assert entry is not None and entry["count"] == 3
        assert verify_trace(settings, trace) == []
        report = verify(settings)
        assert report.ok, report.format()
        assert report.entries == 1 and report.traces == 1
        # Une seconde passe ne duplique rien.
        assert seal_all(settings) == 0
        assert len(_lines(audit_path(settings))) == 1


def test_one_modified_byte_is_detected() -> None:
    with tmpdir("audit-modifie") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        seal_trace(settings, trace)
        lines = _lines(trace)
        lines[1] = lines[1].replace('"index": 1', '"index": 2')
        trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
        problems = verify_trace(settings, trace)
        assert problems and "different" in problems[0]
        assert not verify(settings).ok


def test_deleted_record_is_detected() -> None:
    with tmpdir("audit-supprime") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        seal_trace(settings, trace)
        lines = _lines(trace)
        trace.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        problems = verify_trace(settings, trace)
        assert any("2 enregistrement" in problem for problem in problems)


def test_reordered_records_are_detected() -> None:
    with tmpdir("audit-reordre") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        seal_trace(settings, trace)
        lines = _lines(trace)
        trace.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n", encoding="utf-8")
        assert verify_trace(settings, trace)


def test_policy_decisions_join_the_chain_and_chain_tampering_is_detected() -> None:
    with tmpdir("audit-politique") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        seal_trace(settings, trace)
        decision = record_policy_decision(
            settings, tool="fs_delete", allowed=False, rule="regle 2 (deny)"
        )
        assert decision is not None and decision["kind"] == "policy"
        assert verify(settings).ok

        chain = _lines(audit_path(settings))
        forged = json.loads(chain[1])
        forged["allowed"] = True
        chain[1] = json.dumps(forged)
        audit_path(settings).write_text("\n".join(chain) + "\n", encoding="utf-8")
        report = verify(settings)
        assert not report.ok
        assert any("empreinte invalide" in problem for problem in report.problems)


def test_external_anchor_accepts_append_and_detects_a_rechained_rewrite() -> None:
    with tmpdir("audit-ancre") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        seal_trace(settings, trace)
        record_policy_decision(
            settings, tool="fs_delete", allowed=False, rule="regle 1 (deny)"
        )
        anchor = tmp / "hors-machine" / "chain.jsonl"
        exported = export_anchor(settings, anchor)
        assert exported["entries"] == 2 and exported["head"]
        assert verify_anchor(settings, anchor) == []

        # Un ajout légitime après la copie reste compatible avec l'ancre.
        record_policy_decision(
            settings, tool="shell_run", allowed=False, rule="regle 2 (deny)"
        )
        assert verify(settings, anchor=anchor).ok

        # Réécriture complète et rechaînée : la chaîne interne reste valide,
        # mais le préfixe ne correspond plus à la copie hors machine.
        entries = [json.loads(line) for line in _lines(audit_path(settings))]
        entries[1]["allowed"] = True
        previous = ""
        for index, entry in enumerate(entries, start=1):
            entry["seq"] = index
            entry["prev"] = previous
            entry["hash"] = _entry_hash(entry)
            previous = entry["hash"]
        audit_path(settings).write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n",
            encoding="utf-8",
        )

        assert verify(settings).ok, "la réécriture rechaînée est valide en interne"
        report = verify(settings, anchor=anchor)
        assert not report.ok
        assert any("ancre externe" in problem for problem in report.problems)


def test_missing_sealed_trace_is_reported() -> None:
    with tmpdir("audit-absent") as tmp:
        settings = make_settings(tmp)
        trace = _write_trace(tmp)
        seal_trace(settings, trace)
        trace.unlink()
        report = verify(settings)
        assert not report.ok
        assert any("introuvable" in problem for problem in report.problems)


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
