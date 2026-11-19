"""Corpus partagé de traces abîmées, pour que tous les lecteurs s'accordent.

Les cas difficiles étaient testés séparément ; une divergence entre le lint, le
rejeu, la compaction, la reconstruction et la rotation pouvait donc passer. Ce
module fabrique un dossier reproductible et le rend aux tests.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.events import Event, EventType
from core.run_index import INDEX_NAME
from core.trace import RunTrace

#: Clé fixe du cas chiffré (32 octets).
KEY = bytes(range(32))
#: Clé de destination pour exercer la rotation.
KEY_NEW = bytes(reversed(range(32)))

#: Noms des cas, stables pour les assertions.
HEALTHY = "saine"
TRUNCATED = "coupee"
CORRUPT = "corrompue"
EMPTY = "vide"
ENCRYPTED = "chiffree"
ARCHIVED = "archive"
INTERRUPTED = "interrompue"


def _finished(
    directory: Path,
    run_id: str,
    *,
    encrypt_key: bytes | None = None,
    archive: bool = False,
) -> Path:
    trace = RunTrace(
        directory,
        encrypt_key=encrypt_key,
        archive=archive,
        archive_min_bytes=1 if archive else 0,
    )
    trace(
        Event(
            type=EventType.RUN_STARTED,
            run_id=run_id,
            payload={"model": "m", "trace_format": 3, "workspace": "w"},
            seq=1,
        )
    )
    trace(
        Event(
            type=EventType.RUN_FINISHED,
            run_id=run_id,
            payload={"ok": True, "stop_reason": "final"},
            seq=2,
        )
    )
    assert trace.path is not None
    return trace.path


def build_corpus(root: Path) -> dict[str, Path]:
    """Crée chaque cas dans `root/runs` et retourne {nom: chemin}."""
    directory = root / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    paths[HEALTHY] = _finished(directory, HEALTHY)

    # Coupure en pleine écriture : demi-ligne JSON sans saut final.
    truncated = _finished(directory, TRUNCATED)
    with truncated.open("a", encoding="utf-8") as handle:
        handle.write('{"run_id": "coupee", "type": "tool_sta')
    paths[TRUNCATED] = truncated

    # Corruption au milieu : la ligne fautive est complète (saut final).
    corrupt = _finished(directory, CORRUPT)
    lines = corrupt.read_text(encoding="utf-8").splitlines()
    corrupt.write_text(
        "\n".join([lines[0], "pas du json", lines[1]]) + "\n", encoding="utf-8"
    )
    paths[CORRUPT] = corrupt

    empty = directory / f"{EMPTY}.jsonl"
    empty.write_text("", encoding="utf-8")
    paths[EMPTY] = empty

    paths[ENCRYPTED] = _finished(directory, ENCRYPTED, encrypt_key=KEY)
    paths[ARCHIVED] = _finished(directory, ARCHIVED, archive=True)

    # Run tué : ouverture sans fin, marqueur d'activité périmé.
    interrupted = RunTrace(directory)
    interrupted(
        Event(type=EventType.RUN_STARTED, run_id=INTERRUPTED, payload={"model": "m"}, seq=1)
    )
    interrupted.close()
    assert interrupted.path is not None
    marker = directory / f"{INTERRUPTED}.active"
    marker.write_text("", encoding="utf-8")
    old = time.time() - 3_600
    os.utime(marker, (old, old))
    paths[INTERRUPTED] = interrupted.path

    # Index volontairement abîmé : doublon, état d'un run absent, ligne cassée.
    index_path = directory / INDEX_NAME
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "run_id": HEALTHY,
                    "state": "finished",
                    "updated": "2026-01-01T00:00:02+00:00",
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "run_id": "disparu",
                    "state": "finished",
                    "updated": "2026-01-01T00:00:03+00:00",
                }
            )
            + "\n"
        )
        handle.write('{"run_id": "casse", "state": "sta\n')
    paths["index"] = index_path
    return paths
