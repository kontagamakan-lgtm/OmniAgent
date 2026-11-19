"""Rotation de la clé des traces chiffrées.

Chaque fichier est relu avec l'ancienne clé puis réécrit avec la nouvelle dans
un temporaire du même dossier, remplacé atomiquement (`os.replace`). Un échec
en cours de route — clé absente, ligne illisible, écriture refusée — laisse le
fichier d'origine intact : aucun run n'est perdu, et l'opération est reprise
possible puisque les fichiers déjà tournés sont reconnus par la nouvelle clé.

Les traces en clair ne sont pas touchées (elles ne sont pas sous clé), un run
dont le marqueur d'activité est frais est ignoré, et l'index est rafraîchi
(taille, chemin) puis compacté : l'état, le modèle et la raison d'arrêt des
runs sont conservés.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any

from core.run_index import INDEX_NAME, RunIndex
from core.trace_crypto import (
    TraceKeyError,
    decrypt_record,
    encrypt_record,
    is_encrypted,
)

__all__ = [
    "NEXT_KEY_ENV",
    "RotationError",
    "rotate_traces",
]

#: Variable proposée pour la nouvelle clé, le temps de la rotation.
NEXT_KEY_ENV = "OMNIAGENT_TRACE_KEY_NEW"
#: Au-delà, un marqueur d'activité est périmé et ne protège plus le fichier.
DEFAULT_ACTIVE_TTL_S = 300.0

#: États rapportés, dans l'ordre où ils apparaissent dans la sortie.
ROTATED = "rotated"
ALREADY = "already"
PLAINTEXT = "plaintext"
ACTIVE = "active"
FAILED = "failed"


class RotationError(RuntimeError):
    """Fichier de trace impossible à relire ou à réécrire sans perte."""


def _run_id(path: Path) -> str:
    name = path.name[:-3] if path.name.endswith(".gz") else path.name
    return name[:-6] if name.endswith(".jsonl") else name


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Tous les enregistrements d'un fichier, refusant la moindre ligne illisible."""
    opener = (
        gzip.open(path, "rt", encoding="utf-8")
        if path.name.endswith(".gz")
        else path.open(encoding="utf-8")
    )
    records: list[dict[str, Any]] = []
    with opener as handle:
        for number, line in enumerate(handle, start=1):
            body = line.strip()
            if not body:
                raise RotationError(f"ligne {number} vide : fichier non réécrit")
            try:
                data = json.loads(body)
            except ValueError as exc:
                raise RotationError(
                    f"ligne {number} illisible : fichier non réécrit"
                ) from exc
            if not isinstance(data, dict):
                raise RotationError(
                    f"ligne {number} n'est pas un objet JSON : fichier non réécrit"
                )
            records.append(data)
    if not records:
        raise RotationError("aucun enregistrement : fichier non réécrit")
    return records


def _rewrite(path: Path, records: list[dict[str, Any]], key: bytes) -> None:
    """Réécrit le fichier dans un temporaire puis le remplace d'un coup."""
    temp = path.with_name(path.name + ".rotate.tmp")
    try:
        original = path.stat()
        lines = [
            json.dumps(encrypt_record(record, key), ensure_ascii=False, default=str)
            + "\n"
            for record in records
        ]
        if path.name.endswith(".gz"):
            with gzip.open(temp, "wt", encoding="utf-8") as sink:
                sink.writelines(lines)
        else:
            with temp.open("w", encoding="utf-8") as sink:
                sink.writelines(lines)
        # `r+b` : Windows refuse `fsync` sur un descripteur ouvert en lecture.
        with temp.open("r+b") as handle:
            os.fsync(handle.fileno())
        # La rotation ne doit pas rajeunir la trace pour la rétention par âge.
        os.utime(temp, (original.st_atime, original.st_mtime))
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _rotate_file(
    path: Path, old_key: bytes, new_key: bytes, active_ttl_s: float
) -> tuple[str, str]:
    """Retourne (état, détail) sans jamais laisser un fichier à moitié écrit."""
    marker = path.parent / f"{_run_id(path)}.active"
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age <= active_ttl_s:
        return ACTIVE, "marqueur d'activité frais : run en cours"

    try:
        records = _read_records(path)
    except (OSError, EOFError, gzip.BadGzipFile, RotationError) as exc:
        return FAILED, str(exc)

    encrypted = [is_encrypted(record) for record in records]
    if not any(encrypted):
        return PLAINTEXT, "trace en clair : inchangée"
    if not all(encrypted):
        return FAILED, "trace mêlant lignes claires et chiffrées : refusée"

    try:
        clear = [decrypt_record(record, old_key) for record in records]
    except TraceKeyError as old_failure:
        try:
            [decrypt_record(record, new_key) for record in records]
        except TraceKeyError:
            return FAILED, f"ni l'ancienne ni la nouvelle clé : {old_failure}"
        return ALREADY, "déjà chiffrée avec la nouvelle clé"

    try:
        _rewrite(path, clear, new_key)
    except OSError as exc:
        return FAILED, f"réécriture impossible : {exc}"
    return ROTATED, ""


def _targets(root: Path) -> list[Path]:
    """Le dossier et ses sous-dossiers d'espace de travail."""
    targets = [root]
    try:
        subs = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return targets
    for sub in subs:
        try:
            if (sub / INDEX_NAME).exists() or any(sub.glob("*.jsonl*")):
                targets.append(sub)
        except OSError:
            continue
    return targets


def _refresh_indexes(directory: Path, paths: list[Path]) -> None:
    """Met à jour taille et chemin des runs tournés, puis compacte l'index."""
    index = RunIndex(directory)
    entries = index.entries()
    for path in paths:
        record = entries.get(_run_id(path))
        if record is None:
            continue
        try:
            record.bytes = path.stat().st_size
        except OSError:
            continue
        record.path = str(path)
        index.append(record)
    index.compact()


def rotate_traces(
    directory: Path | str,
    old_key: bytes,
    new_key: bytes,
    *,
    active_ttl_s: float = DEFAULT_ACTIVE_TTL_S,
) -> list[dict[str, str]]:
    """Réécrit les traces chiffrées avec `new_key`, fichier par fichier.

    Retourne un bilan par fichier (`path`, `status`, `detail`). Un échec ne
    touche que le fichier concerné : les autres sont traités normalement.
    """
    root = Path(directory)
    report: list[dict[str, str]] = []
    for target in _targets(root):
        try:
            files = sorted(
                path
                for path in target.glob("*.jsonl*")
                if path.is_file() and path.name != INDEX_NAME
            )
        except OSError:
            continue
        rotated: list[Path] = []
        for path in files:
            status, detail = _rotate_file(path, old_key, new_key, active_ttl_s)
            if status == ROTATED:
                rotated.append(path)
            report.append({"path": str(path), "status": status, "detail": detail})
        if rotated:
            _refresh_indexes(target, rotated)
    return report
