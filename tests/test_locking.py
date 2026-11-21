"""Verrou inter-processus : deux écrivains ne perdent pas de lignes."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from support import ROOT, tmpdir  # noqa: I001

from core import run_index
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


def test_a_transient_error_does_not_lose_the_state(monkeypatch) -> None:
    """Un échec passager d'accès au fichier ne doit pas perdre une écriture.

    Antivirus, indexeur et sauvegardes ouvrent brièvement `runs.index` en
    partage exclusif : l'écriture échoue quelques millisecondes, puis
    fonctionne. Renoncer au premier essai faisait disparaître l'état d'un run
    sans que personne ne le sache (49 lignes sur 50 en CI).
    """
    with tmpdir("verrou-erreur-passagere") as tmp:
        directory = Path(tmp)
        real = run_index.file_lock
        attempts = {"count": 0}

        def flaky(path, **kwargs):
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise PermissionError(13, "fichier tenu par un autre programme")
            return real(path, **kwargs)

        monkeypatch.setattr(run_index, "file_lock", flaky)
        index = RunIndex(directory)

        assert index.mark("run-1", "started") is True
        assert attempts["count"] == 3, attempts
        assert set(index.entries()) == {"run-1"}


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
