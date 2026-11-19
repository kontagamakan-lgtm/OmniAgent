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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from config import APP_ID
from core.events import Event, EventType
from security.redaction import redactor

__all__ = ["RunTrace"]

logger = logging.getLogger(f"{APP_ID}.trace")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class RunTrace:
    """Abonné de bus qui écrit `<directory>/<run_id>.jsonl`."""

    def __init__(
        self,
        directory: Path | str,
        *,
        sanitize: Callable[[Any], Any] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self._sanitize = sanitize or redactor.redact_data
        self._handle: TextIO | None = None
        self._run_id = ""
        self.path: Path | None = None

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
            self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._handle.flush()
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
        self.path = self.directory / f"{name}.jsonl"
        self._handle = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - fermeture best-effort
                logger.debug("fermeture de trace impossible", exc_info=True)
