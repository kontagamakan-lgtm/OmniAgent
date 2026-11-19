"""Sauvegarde et restauration des données critiques.

Une archive contient `config.json`, `vault.enc`, `omniagent.db`, le dossier
`autonomy/` (objectifs, journal, propositions, politique) et les traces
(`logs/runs/`), plus `logs/metrics.jsonl`. Ni le cache, ni le profil navigateur,
ni les téléchargements : ce ne sont pas des données utilisateur.

Format `.ombak` :

* clair : c'est un ZIP ordinaire, avec `manifest.json` à la racine ;
* chiffré (si une phrase secrète est fournie) : `OMNIBAK1` + sel (16 o) +
  nonce (12 o) + AES-256-GCM du ZIP, clé PBKDF2-SHA256 (200 000 tours). Sans la
  phrase, rien n'est lisible — y compris les noms de fichiers.

La restauration vérifie d'abord : archive lisible, manifeste présent, chemins
sans `..`, version de format de trace connue, traces JSONL relisibles (lint
tolérant) quand elles ne sont pas chiffrées. Elle est un aperçu par défaut ;
`apply=True` extrait ensuite, après avoir refusé d'écraser un fichier absent du
manifeste.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import APP_ID, TRACE_FORMAT_VERSION, Settings
__all__ = [
    "BACKUP_MAGIC",
    "PASSPHRASE_ENV",
    "backup_summary",
    "create_backup",
    "inspect_backup",
    "restore_backup",
]

logger = logging.getLogger(f"{APP_ID}.backup")

BACKUP_MAGIC = b"OMNIBAK1"
PASSPHRASE_ENV = "OMNIAGENT_BACKUP_PASSPHRASE"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_KDF_ROUNDS = 200_000

#: Fichiers et dossiers inclus, relatifs au dossier de données.
_INCLUDE_FILES = ("config.json", "vault.enc", "omniagent.db", "logs/metrics.jsonl")
_INCLUDE_DIRS = ("autonomy", "logs/runs")


def _archive_name(path: Path) -> str:
    return path.as_posix()


def _collect(settings: Settings) -> list[Path]:
    data = settings.paths.data_dir
    found: list[Path] = []
    for name in _INCLUDE_FILES:
        candidate = data / name
        if candidate.is_file():
            found.append(candidate)
    for name in _INCLUDE_DIRS:
        root = data / name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith((".lock", ".active", ".tmp")):
                continue
            found.append(path)
    return sorted(found)


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _KDF_ROUNDS)


def _seal(plain: bytes, passphrase: str) -> bytes:
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_derive(passphrase, salt)).encrypt(nonce, plain, None)
    return BACKUP_MAGIC + salt + nonce + ciphertext


def _open(blob: bytes, passphrase: str | None) -> bytes:
    if not blob.startswith(BACKUP_MAGIC):
        return blob
    if not passphrase:
        raise ValueError("archive chiffrée : fournir la phrase secrète")
    start = len(BACKUP_MAGIC)
    salt = blob[start : start + _SALT_BYTES]
    nonce = blob[start + _SALT_BYTES : start + _SALT_BYTES + _NONCE_BYTES]
    ciphertext = blob[start + _SALT_BYTES + _NONCE_BYTES :]
    try:
        return AESGCM(_derive(passphrase, salt)).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001 - mauvaise phrase ou archive altérée
        raise ValueError("phrase secrète incorrecte ou archive altérée") from exc


def create_backup(
    settings: Settings,
    target: Path | str | None = None,
    *,
    passphrase: str | None = None,
) -> Path:
    """Écrit l'archive et retourne son chemin."""
    data = settings.paths.data_dir
    destination = (
        Path(target)
        if target
        else data / "backups" / f"omniagent-{datetime.now().strftime('%Y%m%d-%H%M%S')}.ombak"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = _collect(settings)
    manifest = {
        "format": 1,
        "app": APP_ID,
        "trace_format": TRACE_FORMAT_VERSION,
        "created": datetime.now(tz=timezone.utc).isoformat(),
        "encrypted": bool(passphrase),
        "files": [],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = _archive_name(path.relative_to(data))
            archive.write(path, relative)
            manifest["files"].append(
                {"path": relative, "bytes": path.stat().st_size}
            )
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    blob = buffer.getvalue()
    if passphrase:
        blob = _seal(blob, passphrase)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(blob)
    os.replace(temporary, destination)
    return destination


def _read_archive(source: Path | str, passphrase: str | None) -> tuple[zipfile.ZipFile, dict[str, Any]]:
    blob = Path(source).read_bytes()
    plain = _open(blob, passphrase)
    archive = zipfile.ZipFile(io.BytesIO(plain))
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (KeyError, ValueError) as exc:
        archive.close()
        raise ValueError("manifeste absent ou illisible") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        archive.close()
        raise ValueError("manifeste invalide")
    return archive, manifest


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"chemin interdit dans l'archive : {name}")
    return path


def inspect_backup(
    source: Path | str, *, passphrase: str | None = None
) -> dict[str, Any]:
    """Vérifie une archive sans rien écrire ; retourne un bilan lisible."""
    problems: list[str] = []
    try:
        archive, manifest = _read_archive(source, passphrase)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "problems": [str(exc)], "manifest": {}}
    with archive:
        declared = {str(item.get("path")) for item in manifest["files"] if isinstance(item, dict)}
        present = set(archive.namelist())
        missing = declared - present
        if missing:
            problems.append(f"{len(missing)} fichier(s) du manifeste absents")
        declared_format = int(manifest.get("trace_format") or 0)
        if declared_format > TRACE_FORMAT_VERSION:
            problems.append(
                f"format de trace {declared_format} plus récent que {TRACE_FORMAT_VERSION}"
            )
        for item in manifest["files"]:
            if not isinstance(item, dict):
                continue
            try:
                name = _safe_name(str(item.get("path")))
            except ValueError as exc:
                problems.append(str(exc))
                continue
            if name.suffix in {".lock"} or name.name.endswith(".active"):
                continue
            if name.name.endswith((".jsonl", ".jsonl.gz")):
                try:
                    raw = archive.read(str(name))
                    if name.name.endswith(".gz"):
                        raw = gzip.decompress(raw)
                    lines = raw.decode("utf-8", errors="replace").splitlines()
                    parsed = 0
                    for line in lines:
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("enregistrement non-objet")
                        parsed += 1
                    if not parsed:
                        problems.append(f"{name} : trace vide")
                except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    problems.append(f"{name} : illisible ({exc})")
            elif name.name == "runs.index":
                try:
                    raw = archive.read(str(name)).decode("utf-8", errors="replace")
                    for line in raw.splitlines():
                        if line.strip():
                            json.loads(line)
                except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    problems.append(f"{name} : index illisible ({exc})")
    return {"ok": not problems, "problems": problems, "manifest": manifest}


def backup_summary(result: dict[str, Any]) -> str:
    manifest = result.get("manifest") or {}
    files = manifest.get("files") or []
    total = sum(int(item.get("bytes") or 0) for item in files if isinstance(item, dict))
    lines = [
        f"Archive : {len(files)} fichier(s), {total} octet(s)",
        f"Creation : {manifest.get('created', '?')}",
        f"Chiffree : {'oui' if manifest.get('encrypted') else 'non'}",
        f"Format de trace : {manifest.get('trace_format', '?')}",
    ]
    if result.get("problems"):
        lines.append("Problemes :")
        lines.extend(f"- {problem}" for problem in result["problems"])
    else:
        lines.append("Verification : ok")
    return "\n".join(lines)


def restore_backup(
    settings: Settings,
    source: Path | str,
    *,
    passphrase: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Aperçu ou restauration réelle, après vérification complète."""
    result = inspect_backup(source, passphrase=passphrase)
    if not result.get("ok"):
        return result
    manifest = result["manifest"]
    try:
        archive, _manifest = _read_archive(source, passphrase)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "problems": [str(exc)], "manifest": manifest}
    destination = settings.paths.data_dir
    written: list[str] = []
    with archive:
        for item in manifest["files"]:
            if not isinstance(item, dict):
                continue
            try:
                name = _safe_name(str(item.get("path")))
            except ValueError:
                continue
            target = destination / Path(*name.parts)
            if not apply:
                written.append(str(target))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if name.name.endswith(".jsonl") and not settings.trace.encrypt:
                # Vérifie le JSONL avant de remplacer une trace existante.
                try:
                    with archive.open(str(name)) as handle:
                        lines = handle.read().decode("utf-8", errors="replace").splitlines()
                    for line in lines:
                        if line.strip():
                            json.loads(line)
                except (ValueError, KeyError, OSError) as exc:
                    raise ValueError(f"{name} : trace JSONL invalide ({exc})") from exc
            with archive.open(str(name)) as source_handle, target.open("wb") as sink:
                sink.write(source_handle.read())
            written.append(str(target))
    return {
        "ok": True,
        "problems": [],
        "manifest": manifest,
        "applied": apply,
        "files": written,
    }
