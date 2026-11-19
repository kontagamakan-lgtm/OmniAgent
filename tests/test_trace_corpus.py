"""Un même corpus abîmé, jugé par tous les lecteurs de traces.

L'intérêt n'est pas un cas isolé mais l'accord entre eux : le lint doit voir la
même chose que le rejeu, et la compaction ou la reconstruction doivent retrouver
les états que l'index contenait. Une divergence ici serait exactement le genre
de bug corrigé après coup dans la compaction et la reconstruction.
"""

from __future__ import annotations

import json
from pathlib import Path

from support import tmpdir  # noqa: I001

from core.replay import replay
from core.run_index import RunIndex, scan_interrupted
from core.trace import read_events
from core.trace_lint import lint_file
from core.trace_rotate import rotate_traces
from trace_corpus import (
    ARCHIVED,
    CORRUPT,
    EMPTY,
    ENCRYPTED,
    HEALTHY,
    INTERRUPTED,
    KEY,
    KEY_NEW,
    TRUNCATED,
    build_corpus,
)


def _errors(issues) -> list:
    return [issue for issue in issues if issue.error]


def test_lint_replay_and_rotation_agree_on_the_corpus() -> None:
    with tmpdir("corpus-lecteurs") as tmp:
        paths = build_corpus(Path(tmp))

        assert _errors(lint_file(paths[HEALTHY])[0]) == []
        issues, report = lint_file(paths[TRUNCATED])
        assert _errors(issues) == [], "une coupure finale n'est pas une erreur"
        assert any(issue.code == "unreadable_final_line" for issue in issues)
        assert report.ignored == 1 and report.truncated_final
        assert any(
            issue.code == "unreadable_lines" and issue.error
            for issue in lint_file(paths[CORRUPT])[0]
        ), "une corruption au milieu doit rester une erreur"
        assert _errors(lint_file(paths[EMPTY])[0]), "une trace vide n'est pas valide"
        assert _errors(lint_file(paths[ENCRYPTED], key=KEY)[0]) == []
        assert _errors(lint_file(paths[ENCRYPTED])[0]), "la clé est exigée"

        healthy = replay(paths[HEALTHY])
        assert healthy.ok and healthy.stop_reason == "final"
        truncated = replay(paths[TRUNCATED])
        assert truncated.ok and truncated.report.ignored == 1
        assert not truncated.interrupted
        encrypted = replay(paths[ENCRYPTED], key=KEY)
        assert encrypted.ok and encrypted.model == "m"
        assert [event["type"] for event in read_events(paths[ARCHIVED])].count(
            "run_finished"
        ) == 1


def test_compaction_and_rebuild_converge_on_the_corpus() -> None:
    with tmpdir("corpus-index") as tmp:
        root = Path(tmp)
        paths = build_corpus(root)
        directory = root / "runs"

        marked = scan_interrupted(directory, active_ttl_s=0)
        assert INTERRUPTED in marked
        assert RunIndex(directory).compact() is True

        lines = paths["index"].read_text(encoding="utf-8").splitlines()
        assert lines, "l'index ne doit pas disparaître"
        parsed = [json.loads(line) for line in lines]
        assert len({record["run_id"] for record in parsed}) == len(parsed), (
            "une seule ligne par run après compaction"
        )

        entries = RunIndex(directory).entries()
        terminal = {
            run_id: entry.state
            for run_id, entry in entries.items()
            if run_id in {HEALTHY, TRUNCATED, ENCRYPTED, ARCHIVED, CORRUPT}
        }
        assert terminal == {
            HEALTHY: "finished",
            TRUNCATED: "finished",
            ENCRYPTED: "finished",
            ARCHIVED: "finished",
            CORRUPT: "finished",
        }
        assert entries[INTERRUPTED].state == "interrupted"
        assert all(entry.path for entry in entries.values()), "chaque run a son fichier"
        assert "disparu" not in entries, "une entrée sans trace est purgée"

        rebuilt = RunIndex(directory).rebuild(reset=True)
        rebuilt_terminal = {
            run_id: entry.state
            for run_id, entry in rebuilt.items()
            if run_id in {HEALTHY, TRUNCATED, ENCRYPTED, ARCHIVED, CORRUPT}
        }
        assert rebuilt_terminal == terminal, (
            "la reconstruction doit retrouver les états terminés de l'index"
        )
        # Limite assumée : sans marqueur (`interrupted` n'existe pas dans le
        # fichier), une trace sans fin se relit comme `started`.
        assert rebuilt[INTERRUPTED].state == "started"


def test_rotation_keeps_complete_records_and_reports_the_rest() -> None:
    with tmpdir("corpus-rotation") as tmp:
        root = Path(tmp)
        paths = build_corpus(root)
        directory = root / "runs"

        report = rotate_traces(directory, KEY, KEY_NEW)

        statuses = {Path(item["path"]).name: item["status"] for item in report}
        assert statuses["saine.jsonl"] == "plaintext"
        assert statuses["coupee.jsonl"] == "plaintext"
        assert statuses["corrompue.jsonl"] == "failed"
        assert statuses["vide.jsonl"] == "failed"
        assert statuses["chiffree.jsonl"] == "rotated"
        assert statuses["archive.jsonl.gz"] == "plaintext"

        rotated = list(read_events(paths[ENCRYPTED], key=KEY_NEW))
        assert [record["type"] for record in rotated] == ["run_started", "run_finished"]
        intact = list(read_events(paths[HEALTHY]))
        assert [record["type"] for record in intact] == ["run_started", "run_finished"], (
            "une trace en clair valide reste lisible"
        )
