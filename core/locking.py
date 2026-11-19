"""Verrou fichier inter-processus pour les journaux partagés.

Deux instances (interface + `--autonomy`, ou deux CLI) peuvent écrire le même
`runs.index`, le même journal d'autonomie ou les mêmes métriques. Une écriture
de quelques dizaines d'octets est atomique en pratique, mais une compaction
(réécriture complète) qui s'intercale entre un `append` et son `flush` peut
effacer la ligne de l'autre processus.

Le verrou est un verrou exclusif de fichier (region d'un octet), non bloquant
avec attente bornée : un processus bloqué ne fige pas l'application, il
retourne une erreur claire. Le verrou est relâché à la fermeture du descripteur,
même après une exception.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

__all__ = ["LockTimeout", "file_lock"]


class LockTimeout(TimeoutError):
    """Le verrou n'a pas pu être pris dans le délai imparti."""


#: Un verrou Windows n'est pas réentrant par descripteur : deux acquisitions
#: dans le même thread (append -> prune) doivent partager la même section.
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


def _try_lock(handle: object) -> bool:
    """Tente de prendre le verrou exclusif ; faux si un autre le tient."""
    if os.name == "nt":  # pragma: no cover - exercé par les tests Windows
        import msvcrt

        try:
            handle.seek(0)  # type: ignore[attr-defined]
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def _unlock(handle: object) -> None:
    if os.name == "nt":  # pragma: no cover - exercé par les tests Windows
        import msvcrt

        with contextlib.suppress(OSError):
            handle.seek(0)  # type: ignore[attr-defined]
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


@contextlib.contextmanager
def file_lock(
    path: Path | str, *, timeout: float = 10.0, poll_s: float = 0.05
) -> Iterator[None]:
    """Verrou exclusif sur `path` (créé au besoin), avec attente bornée."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(target)
    if not thread_lock.acquire(timeout=max(0.1, timeout)):
        raise LockTimeout(f"verrou occupé dans ce processus : {target}")
    handle = None
    try:
        handle = target.open("a+b")
        # Windows refuse parfois de verrouiller une région au-delà de la fin
        # d'un fichier vide : un octet sentinelle rend la région valide.
        if target.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, timeout)
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"verrou occupé par un autre processus : {target}")
            time.sleep(max(0.01, poll_s))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        if handle is not None:
            handle.close()
        thread_lock.release()
