"""Rétention sous vraie concurrence : plusieurs processus, un même dossier.

Les autres tests fabriquent les marqueurs d'activité à la main. Celui-ci lance
trois interpréteurs réels qui écrivent en même temps dans le même dossier avec
un `max_runs` minuscule : la propriété défendue est qu'aucun run vivant n'est
purgé et qu'aucune trace n'est tronquée par la rétention d'un voisin.

Lent (démarrage d'interpréteurs), donc lancé en CI via `OMNIAGENT_SLOW_TESTS=1`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import ROOT, tmpdir  # noqa: I001

_CHILD = """
import sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
from core.events import Event, EventType
from core.trace import RunTrace

directory = Path(sys.argv[1])
run_id = sys.argv[2]
ticks = int(sys.argv[3])
trace = RunTrace(directory, max_runs=1, max_age_days=0, max_bytes=0)
trace(Event(type=EventType.RUN_STARTED, run_id=run_id, payload={{"task": run_id}}, seq=1))
for index in range(ticks):
    trace(Event(type=EventType.STEP_STARTED, run_id=run_id, payload={{"index": index}}, seq=index + 2))
    time.sleep(0.02)
trace(Event(type=EventType.RUN_FINISHED, run_id=run_id, payload={{"stop_reason": "final"}}, seq=ticks + 2))
trace.close()
"""


def test_concurrent_processes_keep_their_own_trace() -> None:
    if os.environ.get("OMNIAGENT_SLOW_TESTS") != "1":
        pytest.skip("test lent : lancé par la CI avec OMNIAGENT_SLOW_TESTS=1")

    with tmpdir("concurrence") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        script = Path(tmp) / "child.py"
        script.write_text(_CHILD.format(root=str(ROOT)), encoding="utf-8")

        children = [
            subprocess.Popen(
                [sys.executable, str(script), str(directory), f"run{index}", "20"],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for index in range(3)
        ]
        results = [child.communicate(timeout=120) for child in children]
        codes = [child.returncode for child in children]
        assert codes == [0, 0, 0], [error.decode("utf-8", "replace") for _, error in results]

        for index in range(3):
            path = directory / f"run{index}.jsonl"
            assert path.exists(), f"la trace run{index} a été purgée pendant son écriture"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            types = [record["type"] for record in records]
            assert types[0] == "run_started"
            assert types[-1] == "run_finished", f"run{index} tronqué par un voisin"
            assert len(records) == 22, f"run{index} : {len(records)} enregistrements"

        # Les marqueurs ont tous été retirés à la fermeture.
        assert list(directory.glob("*.active")) == []
