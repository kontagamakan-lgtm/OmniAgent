"""Migration ascendante des traces JSONL (v2 -> v3).

Le lecteur signale un format plus récent mais ne savait rien faire d'un ancien
format. Cette migration comble uniquement l'écart v2 → v3 :

* `payload` absent ou non-objet : enveloppé dans `{"value": ...}` ;
* `seq` absent ou invalide : numérotation croissante à partir de 1 ;
* `run_id` absent : le nom du fichier fait foi ;
* `run_started.payload.trace_format` fixé à 3.

Une trace déjà en v3 n'est pas réécrite. Un format supérieur est refusé. Une
trace chiffrée est ignorée sans clé (on ne peut pas réécrire son payload sans
la déchiffrer) ; avec une clé, elle est déchiffrée puis rechiffrée ligne par
ligne. Avant toute réécriture, une copie `.v2.bak` est créée dans le même
dossier, et le remplacement est atomique.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

from config import TRACE_FORMAT_VERSION
from core.run_index import INDEX_NAME, RunIndex, _run_id_of
from core.trace_crypto import (
    decrypt_record,
    encrypt_record,
    is_encrypted,
)

__all__ = ["migrate_directory", "migrate_file"]


def _version(record: dict[str, Any]) -> int:
    payload = record.get("payload")
    if isinstance(payload, dict):
        value = payload.get("trace_format")
    else:
        value = record.get("trace_format")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _upgrade(record: dict[str, Any], run_id: str, seq: int) -> dict[str, Any]:
    body = dict(record)
    body.setdefault("run_id", run_id)
    payload = body.get("payload")
    if not isinstance(payload, dict):
        payload = {"value": payload}
    if body.get("type") == "run_started":
        payload["trace_format"] = TRACE_FORMAT_VERSION
    body["payload"] = payload
    body["seq"] = seq
    return body


def _read(path: Path) -> list[dict[str, Any]]:
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
                continue
            try:
                data = json.loads(body)
            except ValueError as exc:
                raise ValueError(f"ligne {number} illisible : migration refusee") from exc
            if not isinstance(data, dict):
                raise ValueError(f"ligne {number} n'est pas un objet : migration refusee")
            records.append(data)
    return records


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    temp = path.with_name(path.name + ".migrate.tmp")
    lines = [json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records]
    try:
        if path.name.endswith(".gz"):
            with gzip.open(temp, "wt", encoding="utf-8") as sink:
                sink.writelines(lines)
        else:
            with temp.open("w", encoding="utf-8") as sink:
                sink.writelines(lines)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def migrate_file(
    path: Path | str,
    *,
    apply: bool = False,
    backup: bool = True,
    key: bytes | None = None,
) -> dict[str, Any]:
    """Migre un fichier ; retourne (status, detail) sans exception."""
    target = Path(path)
    try:
        records = _read(target)
    except (OSError, ValueError) as exc:
        return {"path": str(target), "status": "failed", "detail": str(exc)}
    if not records:
        return {"path": str(target), "status": "empty", "detail": "aucun enregistrement"}
    versions = {_version(record) for record in records}
    if any(version > TRACE_FORMAT_VERSION for version in versions):
        return {
            "path": str(target),
            "status": "failed",
            "detail": f"format {max(versions)} plus recent que {TRACE_FORMAT_VERSION}",
        }
    encrypted = any(is_encrypted(record) for record in records)
    if encrypted and key is None:
        return {
            "path": str(target),
            "status": "skipped",
            "detail": "trace chiffree : fournir la cle pour migrer",
        }
    if versions <= {TRACE_FORMAT_VERSION}:
        return {"path": str(target), "status": "current", "detail": "deja en v3"}
    run_id = _run_id_of(target)
    upgraded: list[dict[str, Any]] = []
    for number, record in enumerate(records, start=1):
        clear = decrypt_record(record, key) if encrypted and key is not None else record
        fixed = _upgrade(clear, run_id, number)
        if encrypted and key is not None:
            fixed = encrypt_record(fixed, key)
        upgraded.append(fixed)
    if not apply:
        return {
            "path": str(target),
            "status": "would-migrate",
            "detail": f"format(s) {sorted(versions)} -> {TRACE_FORMAT_VERSION}",
        }
    if backup:
        try:
            target.with_name(target.name + ".v2.bak").write_bytes(target.read_bytes())
        except OSError as exc:
            return {"path": str(target), "status": "failed", "detail": f"copie impossible : {exc}"}
    try:
        _write(target, upgraded)
    except OSError as exc:
        return {"path": str(target), "status": "failed", "detail": f"ecriture impossible : {exc}"}
    try:
        # Rafraichit taille et chemin sans inventer un etat : un run termine
        # doit rester `finished` apres migration.
        index = RunIndex(target.parent)
        entry = index.entries().get(run_id)
        if entry is not None:
            entry.bytes = target.stat().st_size
            entry.path = str(target)
            entry.detail = "migre en v3"
            index.append(entry)
    except Exception:  # noqa: BLE001 - l'index est un cache
        pass
    return {
        "path": str(target),
        "status": "migrated",
        "detail": f"{len(upgraded)} enregistrement(s) en v3",
    }


def migrate_directory(
    directory: Path | str, *, apply: bool = False, key: bytes | None = None
) -> list[dict[str, Any]]:
    """Migre la racine et les sous-dossiers d'espaces de travail."""
    root = Path(directory)
    report: list[dict[str, Any]] = []
    targets = [root]
    try:
        targets += sorted({path.parent for path in root.glob(f"*/{INDEX_NAME}")})
    except OSError:
        pass
    for target in targets:
        try:
            files = sorted(
                path
                for path in target.glob("*.jsonl*")
                if path.is_file() and path.name != INDEX_NAME
            )
        except OSError:
            continue
        for path in files:
            report.append(migrate_file(path, apply=apply, key=key))
    return report
