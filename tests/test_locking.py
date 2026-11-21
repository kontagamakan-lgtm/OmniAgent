"""Verrou inter-processus : deux écrivains ne perdent pas de lignes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from support import ROOT, tmpdir  # noqa: I001

from core import run_index
from core.locking import LockTimeout, file_lock
from core.run_index import RunIndex

#: Ouvre un fichier sans partage, comme le font antivirus et sauvegardes :
#: tant qu'il le tient, toute autre ouverture echoue avec une erreur de
#: partage (PermissionError cote Python). La duree est courte — c'est le mode
#: de panne reel — mais suffisante pour faire echouer un premier essai.
_HOLD_EXCLUSIVE = """
import ctypes
import sys
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]

path = sys.argv[1]
deadline = time.monotonic() + 0.6
held = 0
while time.monotonic() < deadline:
    handle = k32.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        time.sleep(0.005)
        continue
    held += 1
    time.sleep(0.03)
    k32.CloseHandle(handle)
    time.sleep(0.04)
print(held)
"""

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


def test_an_exhausted_retry_records_the_lost_state(monkeypatch) -> None:
    """Une perte definive doit etre visible, pas seulement journalisee.

    Le diagnostic (`--index-doctor`) signale ensuite ces etats : sans trace
    ecrite, une ligne perdue restait invisible jusqu'a ce que quelqu'un
    remarque l'absence du run.
    """
    with tmpdir("verrou-perte-visible") as tmp:
        directory = Path(tmp)
        monkeypatch.setattr(
            run_index,
            "file_lock",
            lambda *args, **kwargs: (_ for _ in ()).throw(LockTimeout("tenu")),
        )
        index = RunIndex(directory)

        assert index.mark("run-perdu", "started") is False
        lost = index.lost_records()
        assert [item["run_id"] for item in lost] == ["run-perdu"]
        assert lost[0]["state"] == "started"


def test_a_held_index_does_not_lose_writes_during_the_interference() -> None:
    """Le vrai mode de panne : un autre programme tient le fichier.

    Antivirus, indexeur ou sauvegarde ouvrent `runs.index` sans partage
    pendant quelques dizaines de millisecondes : toute ecriture echoue avec
    `PermissionError`. Le test de regression simule cet echec en remplacant
    `file_lock` ; celui-ci le provoque reellement, depuis un autre processus.
    """
    if os.environ.get("OMNIAGENT_SLOW_TESTS") != "1":
        pytest.skip("test lent : lance par la CI avec OMNIAGENT_SLOW_TESTS=1")
    if os.name != "nt":
        pytest.skip("le partage exclusif de fichier est propre a Windows")
    with tmpdir("verrou-interference") as tmp:
        directory = Path(tmp)
        index = RunIndex(directory)
        index.mark("amorce", "started")
        target = index.path

        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_EXCLUSIVE, str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            for number in range(10):
                assert index.mark(f"run-{number}", "started") is True, number
                time.sleep(0.01)
        finally:
            out, err = holder.communicate(timeout=30)
        assert holder.returncode == 0, err.decode("utf-8", errors="replace")
        assert int(out.strip() or b"0") > 0, "le processus n'a jamais tenu le fichier"

        assert index.lost_records() == []
        assert len(index.entries()) == 11


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
