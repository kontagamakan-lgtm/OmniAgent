"""Verrou inter-processus : deux écrivains ne perdent pas de lignes."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from support import ROOT, tmpdir  # noqa: I001

from core.locking import LockTimeout, file_lock
from core.run_index import RunIndex

_SCRIPT = """
import sys, time
sys.path.insert(0, sys.argv[1])
from core.run_index import RunIndex
index = RunIndex(sys.argv[2])
for number in range(25):
    index.mark(f"{sys.argv[3]}-{number}", "started")
time.sleep(0.05)
"""


def test_two_processes_append_without_losing_a_line() -> None:
    with tmpdir("verrou-index") as tmp:
        directory = Path(tmp)
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", _SCRIPT, str(ROOT), str(directory), name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for name in ("a", "b")
        ]
        for worker in workers:
            _out, err = worker.communicate(timeout=60)
            assert worker.returncode == 0, err.decode("utf-8", errors="replace")

        entries = RunIndex(directory).entries()

        assert len(entries) == 50
        assert set(entries) == {
            f"{name}-{number}" for name in ("a", "b") for number in range(25)
        }


def test_a_held_lock_times_out_instead_of_blocking_forever() -> None:
    with tmpdir("verrou-attente") as tmp:
        target = Path(tmp) / "shared.lock"
        ready = Path(tmp) / "ready"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.path.insert(0, sys.argv[1]); "
                "from core.locking import file_lock; "
                "from pathlib import Path; "
                "lock=file_lock(Path(sys.argv[2])); lock.__enter__(); "
                "Path(sys.argv[3]).write_text('x'); time.sleep(3)",
                str(ROOT),
                str(target),
                str(ready),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "le processus detenteur n'a pas demarre"
            try:
                with file_lock(target, timeout=0.2):
                    raise AssertionError("le verrou ne devait pas être pris")
            except LockTimeout:
                pass
        finally:
            holder.terminate()
            holder.wait(timeout=10)
