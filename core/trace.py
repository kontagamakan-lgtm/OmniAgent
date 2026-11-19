"""Trace JSONL : un fichier par exécution, payload expurgé.

Le bus ne connaît pas le `run_id` à l'abonnement : le fichier est donc ouvert au
premier événement d'une exécution (`run_started`) et refermé sur `run_finished`.
Chaque ligne contient l'identifiant, l'horodatage, le type et le payload — ce
dernier passé au nettoyeur de secrets, car une trace est un fichier durable,
pas un affichage transitoire.

Une panne d'écriture ne doit jamais interrompre l'exécution : elle est
journalisée et la trace se referme.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from config import APP_ID
from core.events import Event, EventType
from security.redaction import redactor

__all__ = ["RunTrace", "read_events"]

logger = logging.getLogger(f"{APP_ID}.trace")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")

#: Rétention par défaut : assez pour comparer quelques sessions, sans laisser
#: grossir indéfiniment `data/logs/runs`.
DEFAULT_MAX_RUNS = 50
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def read_events(path: Path | str) -> Iterator[dict[str, Any]]:
    """Événements d'une trace, en ignorant les lignes illisibles.

    Une coupure en pleine écriture laisse une demi-ligne JSON en fin de fichier :
    le lecteur doit rendre les événements valides précédents, pas échouer.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.debug("ligne de trace ignorée", exc_info=True)
                continue
            if isinstance(record, dict):
                yield record


class RunTrace:
    """Abonné de bus qui écrit `<directory>/<run_id>.jsonl`."""

    def __init__(
        self,
        directory: Path | str,
        *,
        sanitize: Callable[[Any], Any] | None = None,
        max_runs: int = DEFAULT_MAX_RUNS,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self._sanitize = sanitize or redactor.redact_data
        self.max_runs = max(1, int(max_runs))
        self.max_age_days = max(0.0, float(max_age_days))
        self.max_bytes = max(0, int(max_bytes))
        self._handle: TextIO | None = None
        self._run_id = ""
        self.path: Path | None = None
        self._bytes_written = 0
        #: Vrai si le plafond de taille a interrompu l'écriture de ce run.
        self.truncated = False

    def __call__(self, event: Event) -> None:
        try:
            if event.type == EventType.RUN_STARTED:
                self._open(event.run_id)
            if self._handle is None or event.run_id != self._run_id:
                return
            record = {
                "run_id": event.run_id,
                "timestamp": datetime.fromtimestamp(
                    event.timestamp, tz=timezone.utc
                ).isoformat(),
                "type": event.type,
                "seq": event.seq,
                "payload": self._sanitize(event.payload),
            }
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            if self.max_bytes and self._bytes_written + len(line.encode("utf-8")) > self.max_bytes:
                # Le plafond s'applique aussi au fichier courant : un run très
                # bavard ne doit pas dépasser la taille prévue à lui seul.
                if not self.truncated:
                    self.truncated = True
                    logger.warning(
                        "trace interrompue à %d octets ; la suite de %s n'est pas écrite",
                        self.max_bytes,
                        event.run_id,
                    )
            else:
                self._handle.write(line)
                self._handle.flush()
                self._bytes_written += len(line.encode("utf-8"))
        except Exception:  # noqa: BLE001 - une trace ne casse pas une exécution
            logger.exception("trace impossible pour %s", event.type)
            self.close()
        if event.type == EventType.RUN_FINISHED:
            self.close()

    def _open(self, run_id: str) -> None:
        self.close()
        self._run_id = str(run_id or "")
        if not self._run_id:
            return
        name = _SAFE_NAME.sub("_", self._run_id)[:80]
        self.directory.mkdir(parents=True, exist_ok=True)
        self._prune(keep=f"{name}.jsonl")
        self.path = self.directory / f"{name}.jsonl"
        self._handle = self.path.open("a", encoding="utf-8")
        self.truncated = False
        try:
            self._bytes_written = self.path.stat().st_size
        except OSError:
            self._bytes_written = 0

    def _prune(self, keep: str) -> None:
        """Applique la rétention avant d'ouvrir la trace courante.

        Trois bornes, dans cet ordre : âge, nombre de fichiers, taille totale.
        Le fichier courant est toujours épargné ; une suppression impossible est
        journalisée sans interrompre la nouvelle exécution.
        """
        try:
            candidates = [
                path
                for path in self.directory.glob("*.jsonl")
                if path.name != keep and path.is_file()
            ]
        except OSError:
            logger.debug("rétention impossible : dossier illisible", exc_info=True)
            return

        entries: list[tuple[float, int, Path]] = []
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
        entries.sort()  # du plus ancien au plus récent

        if self.max_age_days:
            cutoff = time.time() - self.max_age_days * 86_400
            kept: list[tuple[float, int, Path]] = []
            for entry in entries:
                if entry[0] < cutoff:
                    self._remove(entry[2])
                else:
                    kept.append(entry)
            entries = kept

        current = self.directory / keep
        total = sum(size for _, size, _ in entries)
        try:
            total += current.stat().st_size
        except OSError:
            pass
        for mtime, size, path in list(entries):
            # Le fichier courant compte dans le quota : `max_runs` runs au total.
            over_count = len(entries) >= self.max_runs
            over_size = self.max_bytes > 0 and total > self.max_bytes
            if not (over_count or over_size):
                break
            if self._remove(path):
                entries.remove((mtime, size, path))
                total -= size

    def _remove(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError:
            logger.debug("rétention : suppression impossible de %s", path, exc_info=True)
            return False

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - fermeture best-effort
                logger.debug("fermeture de trace impossible", exc_info=True)
