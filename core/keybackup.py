"""Sauvegarde chiffrée de l'ancienne clé des traces, le temps d'une rotation.

La rotation réécrit tous les fichiers avec la nouvelle clé. Pendant la fenêtre
où une sauvegarde d'archive ancienne peut encore être restaurée, l'ancienne clé
reste nécessaire. La conserver en clair annulerait le chiffrement : elle est
donc scellée par une phrase secrète distincte (PBKDF2-SHA256, AES-256-GCM).

Format : `OMNIKEY1` + sel (16 o) + nonce (12 o) + chiffré. Sans phrase, rien.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["KEY_MAGIC", "open_key_backup", "seal_key"]

KEY_MAGIC = b"OMNIKEY1"
_SALT = 16
_NONCE = 12
_ROUNDS = 200_000


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _ROUNDS)


def seal_key(key: bytes, target: Path | str, passphrase: str) -> Path:
    """Écrit la clé scellée ; refuse une phrase vide."""
    if not str(passphrase or "").strip():
        raise ValueError("phrase secrete vide : sauvegarde de cle refusee")
    salt = os.urandom(_SALT)
    nonce = os.urandom(_NONCE)
    sealed = AESGCM(_derive(passphrase, salt)).encrypt(nonce, bytes(key), None)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(KEY_MAGIC + salt + nonce + sealed)
    os.replace(temporary, path)
    return path


def open_key_backup(source: Path | str, passphrase: str) -> bytes:
    """Rend la clé ; `ValueError` si format ou phrase sont faux."""
    blob = Path(source).read_bytes()
    if not blob.startswith(KEY_MAGIC):
        raise ValueError("sauvegarde de cle non reconnue")
    start = len(KEY_MAGIC)
    salt = blob[start : start + _SALT]
    nonce = blob[start + _SALT : start + _SALT + _NONCE]
    ciphertext = blob[start + _SALT + _NONCE :]
    try:
        return AESGCM(_derive(passphrase, salt)).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("phrase secrete incorrecte") from exc
