"""Chiffrement au repos du contenu des traces (format 3).

Les métadonnées d'un enregistrement (`run_id`, `timestamp`, `type`, `seq`)
restent en clair : la rétention, la détection des interruptions, l'index et le
lint doivent fonctionner sans la clé. Seul le `payload` — tâche, réponses,
arguments et résultats d'outils — est chiffré, une enveloppe AEAD par ligne.

La clé est un secret aléatoire de 32 octets fourni par une variable
d'environnement dédiée (`OMNIAGENT_TRACE_KEY` par défaut), en base64url ou en
hexadécimal :

    python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

Aucun repli en clair : sans clé valide, `build_trace` refuse d'écrire une trace.
Une clé perdue rend les payloads définitivement illisibles ; l'index
(`runs.index`) et les métadonnées, eux, restent consultables.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "ENVELOPE_FIELD",
    "KEY_BYTES",
    "KEY_ENV",
    "TraceKeyError",
    "decrypt_record",
    "encrypt_record",
    "is_encrypted",
    "load_key",
    "optional_key",
]

#: Variable d'environnement par défaut ; `trace.key_env` peut en désigner une autre.
KEY_ENV = "OMNIAGENT_TRACE_KEY"
#: Taille de clé attendue (AES-256).
KEY_BYTES = 32
#: Nonce GCM : 96 bits, la taille recommandée.
NONCE_BYTES = 12
#: Version de l'enveloppe, pour refuser proprement un futur format.
ENVELOPE_VERSION = 1
#: Champ qui remplace `payload` dans une ligne chiffrée.
ENVELOPE_FIELD = "enc"
#: Manifeste recopié en clair : le reconstruire ne doit pas exiger la clé.
MANIFEST_META_FIELDS = ("app_version", "model", "workspace", "trace_format")


class TraceKeyError(RuntimeError):
    """Clé de trace absente, invalide, ou payload impossible à déchiffrer."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: Any) -> bytes:
    if not isinstance(text, str) or not text:
        raise ValueError("valeur base64 absente")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_key(raw: str) -> bytes | None:
    """Décode une clé hexadécimale ou base64url ; `None` si illisible."""
    candidate = raw.strip()
    if len(candidate) == KEY_BYTES * 2:
        try:
            return bytes.fromhex(candidate)
        except ValueError:
            pass
    try:
        return _unb64(candidate)
    except (ValueError, TypeError):
        return None


def load_key(env_name: str = KEY_ENV) -> bytes:
    """Clé de chiffrement lue dans l'environnement, ou `TraceKeyError`."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        raise TraceKeyError(
            f"variable {env_name} absente : clé des traces chiffrées introuvable"
        )
    key = _decode_key(raw)
    if key is None or len(key) != KEY_BYTES:
        raise TraceKeyError(
            f"{env_name} doit contenir {KEY_BYTES} octets en base64url ou en hexadécimal"
        )
    return key


def optional_key(env_name: str = KEY_ENV) -> bytes | None:
    """Clé si la variable est définie, `None` sinon ; refuse une clé invalide."""
    if not os.environ.get(env_name, "").strip():
        return None
    return load_key(env_name)


def is_encrypted(record: dict[str, Any]) -> bool:
    """Vrai si l'enregistrement porte une enveloppe chiffrée."""
    return isinstance(record.get(ENVELOPE_FIELD), dict)


def _aad(record: dict[str, Any]) -> bytes:
    """Données authentifiées : une ligne déplacée d'un run à l'autre échoue."""
    return (
        f"{record.get('run_id', '')}|{record.get('seq', '')}|{record.get('type', '')}"
    ).encode("utf-8")


def encrypt_record(record: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Copie de l'enregistrement dont seul le `payload` est chiffré.

    `run_started` garde en plus un `meta` minimal (version, modèle, espace de
    travail) : l'index des runs se reconstruit sans la clé, et ces champs ne
    sont pas le contenu de la session.
    """
    body = dict(record)
    payload = body.pop("payload", {})
    if not isinstance(payload, dict):
        payload = {"value": payload}
    nonce = os.urandom(NONCE_BYTES)
    clear = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, clear, _aad(body))
    body[ENVELOPE_FIELD] = {
        "v": ENVELOPE_VERSION,
        "nonce": _b64(nonce),
        "ct": _b64(ciphertext),
    }
    if body.get("type") == "run_started":
        body["meta"] = {
            name: payload.get(name, "") for name in MANIFEST_META_FIELDS
        }
    return body


def decrypt_record(record: dict[str, Any], key: bytes) -> dict[str, Any]:
    """Rend l'enregistrement en clair ; `TraceKeyError` si clé ou contenu faux.

    L'authentification GCM vaut contrôle d'intégrité : une ligne altérée ou une
    mauvaise clé échoue, elle ne produit pas un payload partiel.
    """
    envelope = record.get(ENVELOPE_FIELD)
    if not isinstance(envelope, dict):
        raise TraceKeyError("enregistrement sans enveloppe chiffrée")
    version = envelope.get("v")
    if version != ENVELOPE_VERSION:
        raise TraceKeyError(f"version de chiffrement inconnue : {version!r}")
    try:
        nonce = _unb64(envelope.get("nonce"))
        ciphertext = _unb64(envelope.get("ct"))
    except (TypeError, ValueError) as exc:
        raise TraceKeyError("enveloppe chiffrée illisible") from exc
    try:
        clear = AESGCM(key).decrypt(nonce, ciphertext, _aad(record))
    except InvalidTag as exc:
        raise TraceKeyError(
            "déchiffrement impossible : clé incorrecte ou enregistrement altéré"
        ) from exc
    try:
        payload = json.loads(clear)
    except ValueError as exc:
        raise TraceKeyError("payload déchiffré illisible") from exc
    if not isinstance(payload, dict):
        raise TraceKeyError("payload déchiffré n'est pas un objet JSON")
    body = {
        name: value
        for name, value in record.items()
        if name not in (ENVELOPE_FIELD, "meta")
    }
    body["payload"] = payload
    return body
