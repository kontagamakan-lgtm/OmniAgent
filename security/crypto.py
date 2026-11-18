"""Primitives cryptographiques du coffre-fort OmniAgent.

Format de conteneur (binaire, gros-boutiste)
--------------------------------------------
::

    offset  taille  contenu
    0       8       magie  b"OMNIVLT1"
    8       1       version de format (1)
    9       1       identifiant KDF (1 = argon2id, 2 = scrypt)
    10      2       longueur N du bloc de paramètres (uint16)
    12      N       paramètres KDF, JSON UTF-8 compact et trié
    12+N    16      sel
    28+N    12      nonce AES-GCM
    40+N    ...     texte chiffré || tag GCM (16 octets)

L'*ADN* (données authentifiées supplémentaires) transmis à AES-GCM couvre
l'intégralité de l'en-tête — magie, version, paramètres KDF, sel et nonce.
Toute altération d'un seul octet, y compris le remplacement du sel par un sel
plus faible, invalide le tag : le déchiffrement échoue avant toute
interprétation du contenu.

La clé maîtresse (32 octets) n'est jamais persistée : elle est dérivée à la
volée depuis la phrase secrète, conservée en mémoire sous forme de
``bytearray`` (effaçable) et remise à zéro par :func:`wipe`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
from dataclasses import dataclass, replace
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:  # argon2-cffi est la voie recommandée ; scrypt reste un repli fonctionnel
    from argon2.low_level import Type as _Argon2Type
    from argon2.low_level import hash_secret_raw as _argon2_hash_secret_raw

    _ARGON2_AVAILABLE = True
except ImportError:  # pragma: no cover - dépend de l'environnement d'installation
    _ARGON2_AVAILABLE = False

__all__ = [
    "AUTH_TAG_LEN",
    "FORMAT_VERSION",
    "KEY_LEN",
    "MAGIC",
    "NONCE_LEN",
    "SALT_LEN",
    "AuthenticationError",
    "CryptoError",
    "FormatError",
    "Header",
    "KdfParams",
    "assess_passphrase",
    "decrypt_with_key",
    "derive_key",
    "encrypt_with_key",
    "is_argon2_available",
    "new_salt",
    "parse_header",
    "reseal",
    "seal",
    "unseal",
    "wipe",
]

MAGIC = b"OMNIVLT1"
FORMAT_VERSION = 1
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
AUTH_TAG_LEN = 16
MIN_PASSPHRASE_LEN = 12

_KDF_IDS: dict[str, int] = {"argon2id": 1, "scrypt": 2}
_KDF_NAMES: dict[int, str] = {v: k for k, v in _KDF_IDS.items()}
_PREFIX = struct.Struct(">8sBBH")  # magie, version, kdf, longueur des paramètres
_MAX_PARAMS_LEN = 4096

# Bornes de sécurité appliquées aux paramètres *relus depuis un fichier* :
# un conteneur forgé ne doit pas pouvoir imposer un KDF coûtant des Go de RAM.
_MAX_TIME_COST = 16
_MAX_MEMORY_KIB = 1024 * 1024  # 1 Gio
_MAX_PARALLELISM = 16
_MAX_SCRYPT_N = 1 << 20


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------


class CryptoError(Exception):
    """Erreur cryptographique générique."""


class FormatError(CryptoError):
    """Conteneur illisible : magie, version ou structure invalide."""


class AuthenticationError(CryptoError):
    """Échec du tag GCM : phrase secrète erronée ou conteneur altéré."""


# ---------------------------------------------------------------------------
# Paramètres de dérivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KdfParams:
    """Paramètres de dérivation de clé, sérialisés dans l'en-tête."""

    kdf: Literal["argon2id", "scrypt"] = "argon2id"
    time_cost: int = 3
    memory_kib: int = 65536
    parallelism: int = 4
    scrypt_n: int = 32768
    scrypt_r: int = 8
    scrypt_p: int = 1

    def to_dict(self) -> dict[str, Any]:
        if self.kdf == "argon2id":
            return {
                "kdf": "argon2id",
                "t": self.time_cost,
                "m": self.memory_kib,
                "p": self.parallelism,
            }
        return {"kdf": "scrypt", "n": self.scrypt_n, "r": self.scrypt_r, "p": self.scrypt_p}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KdfParams":
        if not isinstance(data, dict):
            raise FormatError("paramètres KDF invalides")
        name = data.get("kdf")
        if name == "argon2id":
            params = cls(
                kdf="argon2id",
                time_cost=int(data.get("t", 3)),
                memory_kib=int(data.get("m", 65536)),
                parallelism=int(data.get("p", 4)),
            )
        elif name == "scrypt":
            params = cls(
                kdf="scrypt",
                scrypt_n=int(data.get("n", 32768)),
                scrypt_r=int(data.get("r", 8)),
                scrypt_p=int(data.get("p", 1)),
            )
        else:
            raise FormatError(f"KDF inconnu: {name!r}")
        params.ensure_sane()
        return params

    def ensure_sane(self) -> None:
        """Refuse les paramètres absurdes (protection contre le déni de service)."""
        if self.kdf == "argon2id":
            if not (1 <= self.time_cost <= _MAX_TIME_COST):
                raise FormatError(f"time_cost hors bornes: {self.time_cost}")
            if not (8 <= self.memory_kib <= _MAX_MEMORY_KIB):
                raise FormatError(f"memory_kib hors bornes: {self.memory_kib}")
            if not (1 <= self.parallelism <= _MAX_PARALLELISM):
                raise FormatError(f"parallelism hors bornes: {self.parallelism}")
        else:
            n = self.scrypt_n
            if n < 1024 or n > _MAX_SCRYPT_N or (n & (n - 1)) != 0:
                raise FormatError(f"scrypt n invalide: {n}")
            if not (1 <= self.scrypt_r <= 32) or not (1 <= self.scrypt_p <= 16):
                raise FormatError("paramètres scrypt invalides")

    def with_fallback(self) -> "KdfParams":
        """Bascule vers scrypt si argon2-cffi est absent."""
        if self.kdf == "argon2id" and not _ARGON2_AVAILABLE:
            return replace(self, kdf="scrypt")
        return self


def is_argon2_available() -> bool:
    """Indique si le KDF recommandé (Argon2id) est utilisable."""
    return _ARGON2_AVAILABLE


def new_salt(length: int = SALT_LEN) -> bytes:
    return os.urandom(length)


# ---------------------------------------------------------------------------
# Dérivation
# ---------------------------------------------------------------------------


def derive_key(passphrase: str | bytes, salt: bytes, params: KdfParams) -> bytes:
    """Dérive une clé de 32 octets depuis la phrase secrète."""
    if len(salt) < 8:
        raise FormatError("sel trop court")
    secret = passphrase.encode("utf-8") if isinstance(passphrase, str) else bytes(passphrase)
    if not secret:
        raise CryptoError("phrase secrète vide")
    params = params.with_fallback()

    if params.kdf == "argon2id":
        if not _ARGON2_AVAILABLE:  # pragma: no cover - défense
            raise CryptoError("argon2-cffi requis pour argon2id")
        return _argon2_hash_secret_raw(
            secret=secret,
            salt=salt,
            time_cost=params.time_cost,
            memory_cost=params.memory_kib,
            parallelism=params.parallelism,
            hash_len=KEY_LEN,
            type=_Argon2Type.ID,
        )

    # scrypt : mémoire = 128 * n * r octets ; maxmem doit la dépasser.
    maxmem = 132 * params.scrypt_n * params.scrypt_r + 1024 * 1024
    return hashlib.scrypt(
        secret,
        salt=salt,
        n=params.scrypt_n,
        r=params.scrypt_r,
        p=params.scrypt_p,
        dklen=KEY_LEN,
        maxmem=maxmem,
    )


def wipe(buffer: bytearray | None) -> None:
    """Écrase un tampon sensible en place (best effort, sans garantie GC)."""
    if not buffer:
        return
    for index in range(len(buffer)):
        buffer[index] = 0


# ---------------------------------------------------------------------------
# Chiffrement par clé directe (utilisé par le trousseau Windows)
# ---------------------------------------------------------------------------


def encrypt_with_key(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Chiffre un blob avec AES-256-GCM ; retourne nonce || chiffré || tag."""
    if len(key) != KEY_LEN:
        raise CryptoError(f"clé de {KEY_LEN} octets attendue")
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    return nonce + ciphertext


def decrypt_with_key(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    """Déchiffre un blob produit par :func:`encrypt_with_key`."""
    if len(key) != KEY_LEN:
        raise CryptoError(f"clé de {KEY_LEN} octets attendue")
    if len(blob) < NONCE_LEN + AUTH_TAG_LEN:
        raise FormatError("blob chiffré trop court")
    nonce, ciphertext = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad or None)
    except InvalidTag as exc:
        raise AuthenticationError("authentification GCM échouée") from exc


# ---------------------------------------------------------------------------
# Conteneur de coffre-fort
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Header:
    """En-tête analysé d'un conteneur."""

    params: KdfParams
    salt: bytes
    nonce: bytes
    aad: bytes
    ciphertext: bytes


def build_header(params: KdfParams, salt: bytes, nonce: bytes) -> bytes:
    body = json.dumps(params.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > _MAX_PARAMS_LEN:
        raise FormatError("bloc de paramètres KDF trop volumineux")
    return (
        _PREFIX.pack(MAGIC, FORMAT_VERSION, _KDF_IDS[params.kdf], len(body))
        + body
        + salt
        + nonce
    )


def parse_header(blob: bytes) -> Header:
    """Analyse et valide l'en-tête sans dériver de clé."""
    if len(blob) < _PREFIX.size + SALT_LEN + NONCE_LEN + AUTH_TAG_LEN:
        raise FormatError("conteneur tronqué")
    magic, version, kdf_id, params_len = _PREFIX.unpack_from(blob, 0)
    if magic != MAGIC:
        raise FormatError("magie invalide : ce fichier n'est pas un coffre OmniAgent")
    if version != FORMAT_VERSION:
        raise FormatError(f"version de format non prise en charge: {version}")
    if kdf_id not in _KDF_NAMES:
        raise FormatError(f"identifiant KDF inconnu: {kdf_id}")
    if params_len == 0 or params_len > _MAX_PARAMS_LEN:
        raise FormatError("longueur de paramètres invalide")

    cursor = _PREFIX.size
    raw_params = blob[cursor : cursor + params_len]
    cursor += params_len
    salt = blob[cursor : cursor + SALT_LEN]
    cursor += SALT_LEN
    nonce = blob[cursor : cursor + NONCE_LEN]
    cursor += NONCE_LEN
    if len(salt) != SALT_LEN or len(nonce) != NONCE_LEN:
        raise FormatError("en-tête tronqué")

    try:
        payload = json.loads(raw_params.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError("paramètres KDF illisibles") from exc
    if not isinstance(payload, dict):
        raise FormatError("paramètres KDF invalides")
    if payload.get("kdf") != _KDF_NAMES[kdf_id]:
        raise FormatError("incohérence entre l'identifiant KDF et les paramètres")

    params = KdfParams.from_dict(payload)
    ciphertext = blob[cursor:]
    if len(ciphertext) < AUTH_TAG_LEN:
        raise FormatError("texte chiffré absent")
    return Header(
        params=params,
        salt=salt,
        nonce=nonce,
        aad=blob[:cursor],
        ciphertext=ciphertext,
    )


def seal(payload: dict[str, Any], passphrase: str, params: KdfParams | None = None) -> bytes:
    """Chiffre `payload` (JSON) et retourne le conteneur complet."""
    active = (params or KdfParams()).with_fallback()
    active.ensure_sane()
    salt = new_salt()
    nonce = os.urandom(NONCE_LEN)
    key = bytearray(derive_key(passphrase, salt, active))
    try:
        header = build_header(active, salt, nonce)
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(bytes(key)).encrypt(nonce, plaintext, header)
    finally:
        wipe(key)
    return header + ciphertext


def unseal(blob: bytes, passphrase: str) -> tuple[dict[str, Any], KdfParams]:
    """Vérifie, déchiffre et désérialise un conteneur."""
    header = parse_header(blob)
    key = bytearray(derive_key(passphrase, header.salt, header.params))
    try:
        plaintext = AESGCM(bytes(key)).decrypt(header.nonce, header.ciphertext, header.aad)
    except InvalidTag as exc:
        raise AuthenticationError(
            "mot de passe maître incorrect ou coffre-fort altéré"
        ) from exc
    finally:
        wipe(key)

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError("contenu déchiffré illisible") from exc
    if not isinstance(payload, dict):
        raise FormatError("contenu déchiffré invalide")
    return payload, header.params


def reseal(blob: bytes, old_passphrase: str, new_passphrase: str) -> bytes:
    """Change la phrase secrète : déchiffre puis rechiffre avec un nouveau sel."""
    payload, params = unseal(blob, old_passphrase)
    return seal(payload, new_passphrase, params)


def open_with_passphrase(
    blob: bytes, passphrase: str
) -> tuple[dict[str, Any], bytearray, KdfParams, bytes]:
    """Déchiffre et retourne aussi la clé dérivée, à réutiliser pour réécrire.

    La clé reste en mémoire (``bytearray`` effaçable) afin que les écritures
    suivantes n'aient pas à repayer le coût du KDF — Argon2id à 64 Mio prend
    plusieurs centaines de millisecondes par dérivation.
    """
    header = parse_header(blob)
    key = bytearray(derive_key(passphrase, header.salt, header.params))
    try:
        plaintext = AESGCM(bytes(key)).decrypt(header.nonce, header.ciphertext, header.aad)
    except InvalidTag as exc:
        wipe(key)
        raise AuthenticationError(
            "mot de passe maître incorrect ou coffre-fort altéré"
        ) from exc

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        wipe(key)  # la clé ne doit pas survivre à un conteneur rejeté
        raise FormatError("contenu déchiffré illisible") from exc
    if not isinstance(payload, dict):
        wipe(key)
        raise FormatError("contenu déchiffré invalide")
    return payload, key, header.params, header.salt


def seal_with_key(
    payload: dict[str, Any], key: bytes | bytearray, params: KdfParams, salt: bytes
) -> bytes:
    """Réécrit un conteneur avec une clé déjà dérivée et un nonce neuf.

    Le sel et les paramètres sont conservés : le fichier reste déchiffrable
    avec la même phrase secrète, sans repayer la dérivation.
    """
    if len(key) != KEY_LEN:
        raise CryptoError(f"clé de {KEY_LEN} octets attendue")
    if len(salt) != SALT_LEN:
        raise FormatError("sel invalide")
    params.ensure_sane()
    nonce = os.urandom(NONCE_LEN)
    header = build_header(params, salt, nonce)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(bytes(key)).encrypt(nonce, plaintext, header)
    return header + ciphertext


# ---------------------------------------------------------------------------
# Qualité de la phrase secrète
# ---------------------------------------------------------------------------


def assess_passphrase(passphrase: str) -> tuple[int, list[str]]:
    """Note la phrase secrète de 0 à 4 et liste les faiblesses détectées.

    Ce n'est pas un estimateur d'entropie formel : le but est d'empêcher les
    choix manifestement dangereux avant la création du coffre.
    """
    issues: list[str] = []
    score = 0

    if len(passphrase) < MIN_PASSPHRASE_LEN:
        issues.append(f"au moins {MIN_PASSPHRASE_LEN} caractères requis")
    else:
        score += 1
    if len(passphrase) >= 20:
        score += 1

    classes = sum(
        (
            any(c.islower() for c in passphrase),
            any(c.isupper() for c in passphrase),
            any(c.isdigit() for c in passphrase),
            any(not c.isalnum() for c in passphrase),
        )
    )
    if classes >= 3:
        score += 1
    else:
        issues.append("mélangez minuscules, majuscules, chiffres et symboles")

    lowered = passphrase.lower()
    weak_fragments = (
        "password", "motdepasse", "azerty", "qwerty", "123456", "admin",
        "omniagent", "letmein", "changeme", "secret",
    )
    if any(fragment in lowered for fragment in weak_fragments):
        issues.append("contient un fragment courant trop devinable")
        score = max(0, score - 1)

    if len(set(passphrase)) < max(4, len(passphrase) // 4):
        issues.append("trop peu de caractères distincts")
        score = max(0, score - 1)

    if not issues:
        score = max(score, 3)
    return min(score, 4), issues


def random_key_b64(length: int = KEY_LEN) -> str:
    """Génère une clé aléatoire encodée en base64 (usage interne)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(length)).decode("ascii")
