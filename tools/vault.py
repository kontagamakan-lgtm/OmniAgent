"""Coffre-fort d'identifiants chiffré.

Modèle de sécurité
------------------
* Un fichier unique ``vault.enc`` contient l'intégralité du coffre, chiffré en
  AES-256-GCM avec une clé dérivée par Argon2id depuis la phrase secrète
  maîtresse (voir :mod:`security.crypto`).
* La phrase secrète n'est **jamais** écrite sur disque ni journalisée. Après
  déverrouillage, seule la clé dérivée reste en mémoire, dans un ``bytearray``
  effacé au verrouillage. Seule exception, explicite et désactivée par défaut :
  ``vault.keyring_fallback`` / ``remember=True`` délègue la phrase au
  Gestionnaire d'identifiants Windows (``remember_in_keyring``).
* Les écritures réutilisent cette clé avec un nonce neuf : pas de nouvelle
  dérivation Argon2id à chaque ajout d'identifiant.
* Le compteur d'échecs est persisté (``vault.state.json``) avec un verrouillage
  temporaire à backoff exponentiel, pour qu'un redémarrage ne remette pas le
  compteur à zéro.
* L'API exposée au modèle de langage ne renvoie **jamais** un mot de passe en
  clair par défaut : ``reveal=False`` masque la valeur. Les mots de passe sont
  injectés dans le navigateur par le code Python, pas transités par le contexte
  du modèle (donc pas envoyés au fournisseur d'API).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from config import APP_ID, Settings
from security.crypto import (
    AuthenticationError,
    CryptoError,
    FormatError,
    KdfParams,
    assess_passphrase,
    open_with_passphrase,
    seal,
    seal_with_key,
    wipe,
)
from security.redaction import Redactor, redactor as default_redactor
from tools.base import FunctionTool, Tool, ToolError, ToolResult, object_schema, param
from tools.file_manager import FileManager, app_file_manager

__all__ = [
    "Credentials",
    "Vault",
    "VaultAuthError",
    "VaultEntry",
    "VaultError",
    "VaultExists",
    "VaultLocked",
    "VaultNotFound",
    "VaultThrottled",
    "build_vault_tools",
    "normalize_service",
    "totp_code",
    "verify_totp",
]

logger = logging.getLogger(f"{APP_ID}.vault")

VAULT_FORMAT = "omniagent.vault"
VAULT_VERSION = 1
MASK = "[MASQUÉ]"
MASK_NOTES = "[notes masquées]"
KEYRING_SERVICE = "OmniAgent-Vault"
KEYRING_ACCOUNT = "master"


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------


class VaultError(ToolError):
    """Erreur du coffre-fort exploitable par l'agent."""

    code = "vault_error"


class VaultLocked(VaultError):
    code = "vault_locked"

    def __init__(self) -> None:
        super().__init__("le coffre-fort est verrouillé : déverrouillez-le dans l'interface")


class VaultAuthError(VaultError):
    code = "vault_auth_failed"


class VaultExists(VaultError):
    code = "vault_exists"


class VaultNotFound(VaultError):
    code = "vault_not_found"


class VaultThrottled(VaultError):
    code = "vault_throttled"


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_service(name: str) -> str:
    """Réduit un nom de service à une clé stable ('https://www.GitHub.com/x' → 'github.com')."""
    text = (name or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip(".").strip()


@dataclass(slots=True)
class VaultEntry:
    """Identifiant stocké."""

    id: str
    service: str
    username: str = ""
    password: str = ""
    url: str = ""
    totp_secret: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    use_count: int = 0
    # Page de connexion du service : sert de cible à la vérification par
    # connexion quand « origine + /login » n'existe pas.
    login_url: str = ""
    # Horodatage du jour où le compte a été créé avec succès sur le site.
    verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service,
            "username": self.username,
            "password": self.password,
            "url": self.url,
            "totp_secret": self.totp_secret,
            "notes": self.notes,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "login_url": self.login_url,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VaultEntry":
        return cls(
            id=str(data.get("id") or secrets.token_hex(8)),
            service=str(data.get("service", "")),
            username=str(data.get("username", "")),
            password=str(data.get("password", "")),
            url=str(data.get("url", "")),
            totp_secret=str(data.get("totp_secret", "")),
            notes=str(data.get("notes", "")),
            tags=[str(tag) for tag in data.get("tags") or []],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            last_used_at=str(data.get("last_used_at", "")),
            use_count=int(data.get("use_count", 0)),
            login_url=str(data.get("login_url", "")),
            verified_at=str(data.get("verified_at", "")),
        )

    def public(self, *, reveal: bool = False, redactor: Any = None) -> dict[str, Any]:
        """Vue sûre par défaut : secrets **et notes** masqués.

        Le champ « notes » est du texte libre : un mot de passe ou un jeton
        collé dedans repartirait sinon vers le modèle. Il n'est donc jamais
        transmis tel quel ; `reveal=True` le restitue pour l'affichage local,
        toujours passé au `Redactor` (celui fourni, sinon celui du module).
        """
        return {
            "id": self.id,
            "service": self.service,
            "username": self.username,
            "password": self.password if reveal else (MASK if self.password else ""),
            "url": self.url,
            "totp": "configuré" if self.totp_secret else "",
            "notes": self._visible_notes(reveal=reveal, redactor=redactor),
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "login_url": self.login_url,
            "verified_at": self.verified_at,
        }

    def _visible_notes(self, *, reveal: bool, redactor: Any = None) -> str:
        if not self.notes:
            return ""
        if not reveal:
            return MASK_NOTES
        # Le Redactor n'est pas optionnel : sans lui, une clé API collée dans
        # les notes ressortirait en clair dès que l'appelant omet l'argument.
        cleaner = redactor if redactor is not None else default_redactor
        text = cleaner.redact(self.notes)
        # Un mot de passe recopié dans les notes ne ressemble à aucun motif
        # connu : le Redactor ne le voit pas. On retire donc explicitement les
        # secrets de cette entrée, y compris sans redactor disponible.
        for secret in (self.password, self.totp_secret):
            if secret and len(secret) >= 4:
                text = text.replace(secret, MASK)
        return text if len(text) <= 500 else text[:497] + "…"


@dataclass(slots=True)
class Credentials:
    """Identifiants en clair, à usage interne du code Python uniquement."""

    service: str
    username: str
    password: str
    url: str = ""
    login_url: str = ""
    totp_secret: str = ""
    entry_id: str = ""

    def __repr__(self) -> str:  # empêche une fuite via les logs ou un traceback
        return f"Credentials(service={self.service!r}, username={self.username!r}, password=<masqué>)"

    __str__ = __repr__


# ---------------------------------------------------------------------------
# TOTP (RFC 6238), sans dépendance externe
# ---------------------------------------------------------------------------


def _b32decode(secret: str) -> bytes:
    normalized = re.sub(r"[\s\-]", "", secret).upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except Exception as exc:  # noqa: BLE001 - secret base32 mal formé
        raise VaultError(f"secret TOTP base32 invalide : {exc}", code="invalid_totp_secret") from exc


def totp_code(
    secret: str,
    *,
    period: int = 30,
    digits: int = 6,
    timestamp: float | None = None,
    offset: int = 0,
) -> str:
    """Code TOTP à 6 chiffres pour l'instant courant (fenêtre de 30 s)."""
    key = _b32decode(secret)
    counter = int((timestamp if timestamp is not None else time.time()) // period) + offset
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    position = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[position : position + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def totp_remaining(*, period: int = 30, timestamp: float | None = None) -> int:
    """Secondes restantes avant le renouvellement du code.

    La troncature entière renvoyait 0 dans la dernière seconde de la fenêtre,
    alors que le code affiché était encore valide : on arrondit au supérieur et
    on garantit au moins 1 tant que la fenêtre courante court.
    """
    now = timestamp if timestamp is not None else time.time()
    return max(1, math.ceil(period - (now % period)))


def verify_totp(
    secret: str,
    code: str,
    *,
    window: int = 1,
    period: int = 30,
    timestamp: float | None = None,
) -> bool:
    """Vérifie un code avec une tolérance de ±`window` fenêtres."""
    candidate = str(code).strip()
    for offset in range(-window, window + 1):
        expected = totp_code(secret, period=period, offset=offset, timestamp=timestamp)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


# ---------------------------------------------------------------------------
# Coffre-fort
# ---------------------------------------------------------------------------


class Vault:
    """Coffre-fort local chiffré, verrouillable et auditable."""

    def __init__(
        self,
        settings: Settings,
        *,
        redactor: Redactor | None = None,
        file_manager: FileManager | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.redactor = redactor or default_redactor
        self.logger = logger_ or logger
        self._file_manager = file_manager
        self.path = settings.paths.vault_file
        self.state_path = settings.paths.data_dir / "vault.state.json"
        self.audit_path = settings.paths.log_dir / "vault_audit.jsonl"

        self._entries: dict[str, VaultEntry] | None = None
        self._key: bytearray | None = None
        self._params: KdfParams | None = None
        self._salt: bytes = b""
        self._unlocked_at: float = 0.0
        self._last_activity: float = 0.0

    @property
    def file_manager(self) -> FileManager:
        """Gestionnaire de fichiers partagé, utilisé pour confiner les écritures."""
        if self._file_manager is None:
            self._file_manager = app_file_manager(self.settings, self.logger)
        return self._file_manager

    # -- état ---------------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def unlocked(self) -> bool:
        return self._entries is not None

    def remaining_seconds(self) -> int:
        """Secondes avant verrouillage automatique (0 si verrouillé)."""
        if not self.unlocked:
            return 0
        deadline = self._last_activity + self.settings.vault.auto_lock_minutes * 60
        return max(0, int(deadline - time.time()))

    def check_timeout(self) -> bool:
        """Verrouille si l'inactivité dépasse le délai ; retourne True si verrouillé."""
        if self.unlocked and self.remaining_seconds() == 0:
            self.lock(reason="inactivité")
            return True
        return False

    # -- cycle de vie -------------------------------------------------------

    def create(self, passphrase: str, *, force: bool = False) -> None:
        """Crée le coffre et le laisse déverrouillé.

        Avec `force=True`, le conteneur existant est copié en
        ``vault.enc.<horodatage>.bak`` **avant** d'être remplacé : une phrase
        secrète oubliée ne détruit pas les identifiants déjà stockés.
        """
        if self.exists and not force:
            raise VaultExists(f"un coffre existe déjà : {self.path}")
        if self.exists and force:
            self._backup_before_overwrite()
        score, issues = assess_passphrase(passphrase)
        if score < 2 and not force:
            raise VaultError(
                "phrase secrète trop faible : " + " ; ".join(issues), code="weak_passphrase"
            )

        params = KdfParams(
            kdf=self.settings.vault.kdf,
            time_cost=self.settings.vault.argon2_time_cost,
            memory_kib=self.settings.vault.argon2_memory_kib,
            parallelism=self.settings.vault.argon2_parallelism,
            scrypt_n=self.settings.vault.scrypt_n,
            scrypt_r=self.settings.vault.scrypt_r,
            scrypt_p=self.settings.vault.scrypt_p,
        )
        payload = {
            "format": VAULT_FORMAT,
            "version": VAULT_VERSION,
            "created_at": _now_iso(),
            "entries": [],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = seal(payload, passphrase, params)
        self._atomic_write(blob)
        self._reset_failures()
        self.unlock(passphrase)
        self._audit("create", success=True)

    def unlock(self, passphrase: str, *, remember: bool = False) -> None:
        """Déchiffre le coffre après vérification du verrouillage temporaire."""
        self._enforce_throttle()
        if not self.exists:
            raise VaultNotFound(f"aucun coffre à {self.path} : créez-le d'abord")

        try:
            blob = self.path.read_bytes()
        except OSError as exc:
            raise VaultError(f"lecture impossible du coffre : {exc}") from exc

        try:
            payload, key, params, salt = open_with_passphrase(blob, passphrase)
        except AuthenticationError as exc:
            attempts = self._register_failure()
            raise VaultAuthError(
                f"mot de passe maître incorrect ({attempts} échec(s))"
            ) from exc
        except (FormatError, CryptoError) as exc:
            raise VaultError(f"coffre-fort corrompu : {exc}", code="vault_corrupt") from exc

        version = payload.get("version")
        if not isinstance(version, int) or version > VAULT_VERSION:
            wipe(key)
            raise VaultError(
                f"version de coffre non prise en charge: {version}", code="vault_version"
            )

        entries: dict[str, VaultEntry] = {}
        for raw in payload.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            entry = VaultEntry.from_dict(raw)
            entries[entry.id] = entry

        self._entries = entries
        self._key = key
        self._params = params
        self._salt = salt
        self._unlocked_at = time.time()
        self._last_activity = self._unlocked_at
        self._reset_failures()
        self._audit("unlock", success=True, entries=len(entries))
        if remember:
            self.remember_in_keyring(passphrase)

    def lock(self, *, reason: str = "manuel") -> None:
        """Efface la clé et vide le cache mémoire."""
        if self._key is not None:
            wipe(self._key)
        had_entries = len(self._entries or {})
        self._key = None
        self._params = None
        self._salt = b""
        self._entries = None
        self._unlocked_at = 0.0
        self._last_activity = 0.0
        self._audit("lock", success=True, reason=reason, entries=had_entries)

    def change_passphrase(self, old_passphrase: str, new_passphrase: str) -> None:
        """Réécrit le coffre avec une nouvelle phrase secrète et un nouveau sel."""
        if not self.exists:
            raise VaultNotFound("aucun coffre à modifier")
        score, issues = assess_passphrase(new_passphrase)
        if score < 2:
            raise VaultError(
                "nouvelle phrase secrète trop faible : " + " ; ".join(issues),
                code="weak_passphrase",
            )
        try:
            payload, stale_key, params, _salt = open_with_passphrase(
                self.path.read_bytes(), old_passphrase
            )
        except AuthenticationError as exc:
            self._register_failure()
            raise VaultAuthError("ancien mot de passe maître incorrect") from exc

        # La clé dérivée n'est utile qu'à la vérification : on l'efface aussitôt
        # plutôt que de la laisser traîner jusqu'au passage du ramasse-miettes.
        try:
            blob = seal(payload, new_passphrase, params)
            self._atomic_write(blob)
        finally:
            wipe(stale_key)
        self.forget_keyring()
        self._audit("change_passphrase", success=True)
        self.lock(reason="changement de phrase secrète")

    def backup(self, destination: str | os.PathLike[str]) -> Path:
        """Copie le fichier chiffré (sauvegarde sûre, sans déchiffrement).

        La destination passe par le confinement des outils fichiers : elle doit
        rester sous une racine autorisée, et aucun fichier existant n'est écrasé.
        """
        if not self.exists:
            raise VaultNotFound("aucun coffre à sauvegarder")
        target = self.file_manager.resolve(destination)
        if target.is_dir():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = target / f"vault-{stamp}.enc"
            counter = 1
            while target.exists():
                target = target.with_name(f"vault-{stamp}-{counter}.enc")
                counter += 1
        elif target.exists():
            raise VaultError(
                f"la sauvegarde écraserait un fichier existant : {target}",
                code="backup_exists",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.path.read_bytes())
        self._audit("backup", success=True, destination=str(target))
        return target

    # -- accès aux entrées --------------------------------------------------

    def _require_unlocked(self) -> dict[str, VaultEntry]:
        self.check_timeout()
        if self._entries is None:
            raise VaultLocked()
        return self._entries

    def _touch(self) -> None:
        self._last_activity = time.time()

    def find(self, service: str, username: str | None = None) -> VaultEntry | None:
        """Retrouve une entrée par service normalisé, puis par similarité."""
        entries = self._require_unlocked()
        key = normalize_service(service)
        exact = [
            entry
            for entry in entries.values()
            if normalize_service(entry.service) == key
            or normalize_service(entry.url) == key
        ]
        if username:
            lowered = username.strip().lower()
            exact = [entry for entry in exact if entry.username.strip().lower() == lowered]
        if exact:
            self._touch()
            return sorted(exact, key=lambda e: e.last_used_at, reverse=True)[0]

        if key:
            fuzzy = [
                entry
                for entry in entries.values()
                if key in normalize_service(entry.service)
                or normalize_service(entry.service) in key
            ]
            if username:
                lowered = username.strip().lower()
                fuzzy = [e for e in fuzzy if e.username.strip().lower() == lowered]
            if len(fuzzy) == 1:
                self._touch()
                return fuzzy[0]
            if len(fuzzy) > 1:
                raise VaultError(
                    f"plusieurs entrées correspondent à '{service}' : "
                    + ", ".join(sorted(e.service for e in fuzzy)),
                    code="ambiguous_service",
                )
        return None

    def record_signup(
        self, service: str, *, created: bool, status: str, evidence: str = ""
    ) -> None:
        """Journalise le verdict d'une inscription (jamais de secret).

        Une création d'identifiant est un événement durable : le journal garde
        le service, le statut et l'indice observé, sans le mot de passe ni le
        corps de la page.
        """
        self._audit(
            "signup",
            success=bool(created),
            service=normalize_service(service) or service,
            status=str(status)[:32],
            evidence=str(evidence)[:300],
        )

    def mark_verified(self, service: str, username: str | None = None) -> VaultEntry | None:
        """Marque l'entrée comme créée/vérifiée sur le site."""
        entry = self.find(service, username)
        if entry is None:
            return None
        entry.verified_at = _now_iso()
        self._save()
        self._audit("verified", success=True, service=entry.service, entry=entry.id)
        return entry

    def public_entry(self, entry: VaultEntry, *, reveal: bool = False) -> dict[str, Any]:
        """Vue publique d'une entrée : secrets et notes masqués par défaut."""
        return entry.public(reveal=reveal, redactor=self.redactor)

    def credentials(
        self, service: str, username: str | None = None, *, count_use: bool = True
    ) -> Credentials:
        """Identifiants en clair pour le code Python (jamais journalisés).

        `count_use=False` sert aux lectures qui ne sont pas un usage réel —
        notamment les connexions de contrôle : elles ne doivent ni gonfler
        `use_count` ni écrire une entrée « credentials » dans l'audit.
        """
        entry = self.find(service, username)
        if entry is None:
            raise VaultNotFound(f"aucun identifiant pour '{service}'")
        if count_use:
            self.record_use(entry.id)
        return Credentials(
            service=entry.service,
            username=entry.username,
            password=entry.password,
            url=entry.url,
            login_url=entry.login_url,
            totp_secret=entry.totp_secret,
            entry_id=entry.id,
        )

    def record_use(self, entry_id: str) -> VaultEntry | None:
        """Compte un usage réel des identifiants et le journalise.

        À appeler seulement quand l'authentification a réussi : une tentative
        ratée n'est pas un usage.
        """
        entry = self._require_unlocked().get(str(entry_id))
        if entry is None:
            return None
        entry.last_used_at = _now_iso()
        entry.use_count += 1
        self._save()
        self._audit("credentials", success=True, service=entry.service, entry=entry.id)
        return entry

    def list_entries(self, query: str = "", *, limit: int = 200) -> list[VaultEntry]:
        entries = self._require_unlocked()
        needle = (query or "").strip().lower()
        result = []
        for entry in entries.values():
            haystack = " ".join(
                [entry.service, entry.username, entry.url, " ".join(entry.tags), entry.notes]
            ).lower()
            if not needle or needle in haystack:
                result.append(entry)
        self._touch()
        return sorted(result, key=lambda e: (e.service.lower(), e.username.lower()))[:limit]

    def add(
        self,
        service: str,
        username: str = "",
        password: str = "",
        *,
        url: str = "",
        notes: str = "",
        totp_secret: str = "",
        tags: Iterable[str] | None = None,
        login_url: str = "",
        overwrite: bool = False,
    ) -> VaultEntry:
        entries = self._require_unlocked()
        if not service.strip():
            raise VaultError("le nom du service est requis", code="invalid_service")
        existing = self.find(service, username or None)
        if existing is not None and not overwrite:
            raise VaultError(
                f"une entrée existe déjà pour '{existing.service}' / '{existing.username}' "
                "(utilisez overwrite=true pour la remplacer)",
                code="duplicate_entry",
            )
        if totp_secret:
            _b32decode(totp_secret)  # valide le secret avant stockage

        now = _now_iso()
        if existing is not None:
            existing.username = username or existing.username
            existing.password = password or existing.password
            existing.url = url or existing.url
            existing.login_url = login_url or existing.login_url
            existing.notes = notes or existing.notes
            existing.totp_secret = totp_secret or existing.totp_secret
            existing.tags = sorted({*(existing.tags), *(tags or [])})
            existing.updated_at = now
            self._save()
            self._audit("update", success=True, service=existing.service, entry=existing.id)
            return existing

        entry = VaultEntry(
            id=secrets.token_hex(8),
            service=service.strip(),
            username=username.strip(),
            password=password,
            url=url.strip(),
            login_url=login_url.strip(),
            totp_secret=totp_secret.strip(),
            notes=notes,
            tags=sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()}),
            created_at=now,
            updated_at=now,
        )
        entries[entry.id] = entry
        self._save()
        self._audit("add", success=True, service=entry.service, entry=entry.id)
        return entry

    def update(self, service: str, *, match_username: str | None = None, **changes: Any) -> VaultEntry:
        entry = self.find(service, match_username)
        if entry is None:
            raise VaultNotFound(f"aucune entrée pour '{service}'")
        allowed = {"username", "password", "url", "login_url", "notes", "totp_secret", "tags"}
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            if key == "totp_secret" and value:
                _b32decode(str(value))
            if key == "tags":
                value = sorted({str(tag).strip() for tag in value if str(tag).strip()})
            setattr(entry, key, value)
        entry.updated_at = _now_iso()
        self._save()
        self._audit("update", success=True, service=entry.service, entry=entry.id)
        return entry

    def delete(self, service: str, username: str | None = None) -> bool:
        entry = self.find(service, username)
        if entry is None:
            return False
        entries = self._require_unlocked()
        entries.pop(entry.id, None)
        self._save()
        self._audit("delete", success=True, service=entry.service, entry=entry.id)
        return True

    def totp(self, service: str, username: str | None = None) -> dict[str, Any]:
        entry = self.find(service, username)
        if entry is None:
            raise VaultNotFound(f"aucune entrée pour '{service}'")
        if not entry.totp_secret:
            raise VaultError(f"aucun secret TOTP pour '{entry.service}'", code="no_totp")
        # Un seul instant de référence : sinon le code et le compte à rebours
        # peuvent provenir de deux fenêtres différentes autour d'une frontière.
        now = time.time()
        return {
            "service": entry.service,
            "code": totp_code(entry.totp_secret, timestamp=now),
            "seconds_remaining": totp_remaining(timestamp=now),
            "period": 30,
        }

    # -- génération de mots de passe ---------------------------------------

    @staticmethod
    def generate_password(length: int = 20, *, symbols: bool = True, avoid_ambiguous: bool = True) -> str:
        """Génère un mot de passe aléatoire cryptographiquement sûr."""
        length = max(8, min(int(length), 128))
        lower = "abcdefghijkmnopqrstuvwxyz" if avoid_ambiguous else "abcdefghijklmnopqrstuvwxyz"
        upper = "ABCDEFGHJKLMNPQRSTUVWXYZ" if avoid_ambiguous else "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        digits = "23456789" if avoid_ambiguous else "0123456789"
        specials = "!@#$%^&*()-_=+[]{};:,.?/"

        alphabet = lower + upper + digits + (specials if symbols else "")
        while True:
            candidate = "".join(secrets.choice(alphabet) for _ in range(length))
            if any(c.islower() for c in candidate) and any(c.isupper() for c in candidate):
                if any(c.isdigit() for c in candidate) and (not symbols or any(c in specials for c in candidate)):
                    return candidate

    # -- statistiques -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        entries = self._entries or {}
        services = {normalize_service(entry.service) for entry in entries.values()}
        with_totp = sum(1 for entry in entries.values() if entry.totp_secret)
        weak = sorted(
            entry.service
            for entry in entries.values()
            if entry.password and len(entry.password) < 10
        )
        return {
            "exists": self.exists,
            "unlocked": self.unlocked,
            "entries": len(entries),
            "services": len(services),
            "with_totp": with_totp,
            "weak_passwords": weak,
            "auto_lock_seconds": self.remaining_seconds() if self.unlocked else 0,
            "kdf": self._params.to_dict() if self._params else None,
        }

    # -- persistance --------------------------------------------------------

    def _save(self) -> None:
        if self._entries is None or self._key is None or self._params is None:
            raise VaultLocked()
        payload = {
            "format": VAULT_FORMAT,
            "version": VAULT_VERSION,
            "updated_at": _now_iso(),
            "entries": [entry.to_dict() for entry in self._entries.values()],
        }
        blob = seal_with_key(payload, self._key, self._params, self._salt)
        self._atomic_write(blob)

    def _backup_before_overwrite(self) -> Path:
        """Copie le conteneur courant avant un remplacement destructif."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.{stamp}.bak")
        counter = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.{stamp}-{counter}.bak")
            counter += 1
        try:
            target.write_bytes(self.path.read_bytes())
        except OSError as exc:
            raise VaultError(
                f"sauvegarde préalable du coffre impossible : {exc}", code="backup_failed"
            ) from exc
        self._audit("create_backup", success=True, destination=str(target))
        self.logger.warning("coffre existant sauvegardé avant écrasement : %s", target)
        return target

    def _atomic_write(self, blob: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)
        try:  # restriction des droits (sans effet réel sur NTFS, utile en portable)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- limitation des tentatives -----------------------------------------

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    def _enforce_throttle(self) -> None:
        state = self._read_state()
        locked_until = float(state.get("locked_until", 0) or 0)
        now = time.time()
        if locked_until > now:
            raise VaultThrottled(
                f"trop de tentatives : réessayez dans {int(locked_until - now)} s"
            )

    def _register_failure(self) -> int:
        state = self._read_state()
        failed = int(state.get("failed", 0)) + 1
        limit = max(1, self.settings.vault.max_failed_unlocks)
        if failed >= limit:
            over = failed - limit
            delay = min(300, 15 * (2 ** min(over, 5)))
            state["locked_until"] = time.time() + delay
        state["failed"] = failed
        self._write_state(state)
        self._audit("unlock_failed", success=False, attempts=failed)
        return failed

    def _reset_failures(self) -> None:
        self._write_state({"failed": 0, "locked_until": 0})

    # -- journal d'audit ----------------------------------------------------

    def _audit(self, action: str, *, success: bool, **fields: Any) -> None:
        if not self.settings.vault.audit_log:
            return
        record = {"ts": _now_iso(), "action": action, "success": success, **fields}
        line = self.redactor.redact(json.dumps(record, ensure_ascii=False))
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            self.logger.debug("journal d'audit du coffre indisponible")

    def read_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        """Dernières entrées d'audit, de la plus récente à la plus ancienne."""
        if not self.audit_path.is_file():
            return []
        lines = self.audit_path.read_text(encoding="utf-8", errors="replace").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(records))

    # -- trousseau Windows --------------------------------------------------

    @staticmethod
    def _keyring_module() -> Any:
        """Retourne le module `keyring`, ou None s'il n'est pas installé."""
        try:
            import keyring
        except ImportError:
            return None
        return keyring

    def _keyring_passphrase(self) -> str | None:
        """Phrase actuellement déposée dans le Gestionnaire d'identifiants."""
        module = self._keyring_module()
        if module is None:
            return None
        try:
            return module.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or None
        except Exception:  # noqa: BLE001 - magasin indisponible
            return None

    def keyring_status(self) -> dict[str, bool]:
        """État du dépôt de la phrase maîtresse dans le trousseau Windows.

        `stale` signale une phrase encore stockée alors que `keyring_fallback`
        est désactivé : reliquat d'une exécution antérieure, à purger.
        """
        enabled = bool(self.settings.vault.keyring_fallback)
        stored = self._keyring_passphrase() is not None
        return {
            "enabled": enabled,
            "available": self._keyring_module() is not None,
            "stored": stored,
            "stale": stored and not enabled,
        }

    def purge_stale_keyring(self, *, confirm: Callable[[], bool] | None = None) -> bool:
        """Supprime une phrase déposée alors que l'option est désactivée.

        Sans `confirm`, rien n'est supprimé tant que l'appelant n'a pas exprimé
        d'accord. Retourne True s'il ne reste plus de reliquat après l'appel.
        """
        if not self.keyring_status()["stale"]:
            return True
        if confirm is not None and not confirm():
            return False
        self.forget_keyring()
        return not self.keyring_status()["stored"]

    def keyring_available(self) -> bool:
        if not self.settings.vault.keyring_fallback:
            return False
        try:
            import keyring  # noqa: F401
        except ImportError:
            return False
        return True

    def remember_in_keyring(self, passphrase: str) -> bool:
        """Mémorise la phrase secrète dans le Gestionnaire d'identifiants Windows."""
        if not self.keyring_available():
            return False
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, passphrase)
            self._audit("keyring_remember", success=True)
            return True
        except Exception as exc:  # noqa: BLE001 - backend indisponible
            self.logger.warning("trousseau indisponible : %s", exc)
            return False

    def forget_keyring(self) -> bool:
        """Supprime la phrase mémorisée, même si l'option a été désactivée depuis.

        La purge ne doit pas dépendre de `keyring_fallback` : sinon, désactiver
        l'option après coup laisserait la phrase dans le Gestionnaire
        d'identifiants Windows sans aucun moyen de l'en retirer.
        """
        try:
            import keyring
        except ImportError:
            return False
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            self._audit("keyring_forget", success=True)
            return True
        except Exception:  # noqa: BLE001 - absence d'entrée
            return False

    def unlock_from_keyring(self) -> bool:
        """Tente un déverrouillage silencieux via le trousseau Windows."""
        if not self.keyring_available():
            return False
        try:
            import keyring

            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:  # noqa: BLE001
            return False
        if not stored:
            return False
        try:
            self.unlock(stored)
            return True
        except VaultError:
            return False

    # -- verrouillage ----------------------------------------------

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.lock(reason="fin de contexte")

    def __del__(self) -> None:  # pragma: no cover - nettoyage best effort
        try:
            if self._key is not None:
                wipe(self._key)
        except Exception:  # noqa: BLE001 - interpréteur en cours d'arrêt
            pass


# ---------------------------------------------------------------------------
# Outils exposés au modèle
# ---------------------------------------------------------------------------


def build_vault_tools(vault: Vault) -> list[Tool]:
    """Construit la liste d'outils `vault_*` liés à cette instance."""

    def vault_status() -> dict[str, Any]:
        return vault.stats()

    def vault_services() -> dict[str, Any]:
        entries = vault.list_entries()
        return {
            "count": len(entries),
            "services": [
                {
                    "service": entry.service,
                    "username": entry.username,
                    "totp": bool(entry.totp_secret),
                    "tags": entry.tags,
                }
                for entry in entries
            ],
        }

    def vault_list_entries(query: str = "", limit: int = 50) -> dict[str, Any]:
        entries = vault.list_entries(query, limit=limit)
        return {"count": len(entries), "entries": [vault.public_entry(entry) for entry in entries]}

    def vault_get_entry(service: str, field_name: str = "password", reveal: bool = False) -> dict[str, Any]:
        entry = vault.find(service)
        if entry is None:
            raise VaultNotFound(f"aucune entrée pour '{service}'")
        mapping = {
            "username": entry.username,
            "password": entry.password,
            "url": entry.url,
            "notes": entry.notes,
            "totp_secret": entry.totp_secret,
        }
        if field_name not in mapping:
            raise VaultError(
                f"champ inconnu '{field_name}' (attendu: {', '.join(mapping)})", code="invalid_field"
            )
        value = mapping[field_name]
        if not reveal and field_name in {"password", "totp_secret"}:
            return {
                "service": entry.service,
                "field": field_name,
                "value": MASK,
                "hint": "utilisez login_to_service pour injecter le secret sans l'afficher",
            }
        payload = {"service": entry.service, "field": field_name, "value": value}
        if field_name in {"password", "totp_secret"}:
            # Révélation explicite : `to_text()` refuse alors de sérialiser le
            # résultat, donc le secret ne part pas vers le fournisseur LLM.
            return ToolResult(data=payload, sensitive=True)
        return payload

    def vault_create_credential(
        service: str,
        username: str = "",
        password_length: int = 20,
        symbols: bool = True,
        url: str = "",
        notes: str = "",
        tags: list[str] | None = None,
        totp_secret: str = "",
        login_url: str = "",
    ) -> ToolResult:
        """Génère un mot de passe fort et l'enregistre immédiatement.

        Le flux « créer un compte » ne peut pas passer par un mot de passe que
        le modèle verrait : la valeur est donc écrite dans le coffre dans le même
        appel, et seul un résumé remonte.
        """
        generated = Vault.generate_password(password_length, symbols=symbols)
        entry = vault.add(
            service,
            username,
            generated,
            url=url,
            notes=notes,
            totp_secret=totp_secret,
            tags=tags or [],
            login_url=login_url,
        )
        return ToolResult(
            data={
                "service": entry.service,
                "username": entry.username,
                "length": len(generated),
                "entry": vault.public_entry(entry),
            },
            sensitive=True,
            safe_summary=(
                f"identifiant créé pour {entry.service} "
                f"({entry.username or 'sans identifiant'}) avec un mot de passe de "
                f"{len(generated)} caractères enregistré dans le coffre ; "
                "valeur non transmise au modèle."
            ),
        )

    def vault_add_entry(
        service: str,
        username: str = "",
        password: str = "",
        url: str = "",
        login_url: str = "",
        notes: str = "",
        tags: list[str] | None = None,
        totp_secret: str = "",
        overwrite: bool = False,
    ) -> ToolResult:
        entry = vault.add(
            service,
            username,
            password,
            url=url,
            login_url=login_url,
            notes=notes,
            totp_secret=totp_secret,
            tags=tags or [],
            overwrite=overwrite,
        )
        # Le résultat porte les champs masqués : il reste local. Le modèle reçoit
        # un résumé sans secret, assez pour savoir que l'écriture a réussi.
        return ToolResult(
            data={"ok": True, "entry": vault.public_entry(entry)},
            sensitive=True,
            safe_summary=(
                f"identifiant enregistré pour {entry.service} "
                f"({entry.username or 'sans identifiant'}) ; mot de passe et notes "
                "non transmis au modèle."
            ),
        )

    def vault_update_entry(
        service: str,
        username: str = "",
        new_username: str = "",
        password: str = "",
        url: str = "",
        login_url: str = "",
        notes: str = "",
        tags: list[str] | None = None,
    ) -> ToolResult:
        entry = vault.update(
            service,
            match_username=username or None,
            username=new_username or None,
            password=password or None,
            url=url or None,
            login_url=login_url or None,
            notes=notes or None,
            tags=tags,
        )
        return ToolResult(
            data={"ok": True, "entry": vault.public_entry(entry)},
            sensitive=True,
            safe_summary=(
                f"entrée mise à jour pour {entry.service} "
                f"({entry.username or 'sans identifiant'}) ; valeurs non transmises "
                "au modèle."
            ),
        )

    def vault_delete_entry(service: str, username: str = "") -> dict[str, Any]:
        deleted = vault.delete(service, username or None)
        if not deleted:
            raise VaultNotFound(f"aucune entrée pour '{service}'")
        return {"ok": True, "deleted": service}

    def vault_generate_password(password_length: int = 20, symbols: bool = True) -> ToolResult:
        generated = Vault.generate_password(password_length, symbols=symbols)
        classes = ["majuscules", "minuscules", "chiffres"]
        if symbols:
            classes.append("symboles")
        # Le mot de passe produit est destiné à devenir un identifiant : sa
        # valeur reste locale, le modèle n'en reçoit que les caractéristiques.
        return ToolResult(
            data={"password": generated, "length": len(generated)},
            sensitive=True,
            safe_summary=(
                f"mot de passe de {len(generated)} caractères généré "
                f"({', '.join(classes)}) ; valeur non transmise au modèle."
            ),
        )

    def vault_totp(service: str, username: str = "") -> ToolResult:
        payload = vault.totp(service, username or None)
        # Un code TOTP est un facteur d'authentification actif : le modèle sait
        # qu'il existe et combien de temps il reste valable, sans le voir.
        return ToolResult(
            data=payload,
            sensitive=True,
            safe_summary=(
                f"code 2FA pour {payload['service']} disponible et valable "
                f"{payload['seconds_remaining']} s sur {payload['period']} ; "
                "code non transmis au modèle."
            ),
        )

    def vault_lock() -> dict[str, Any]:
        vault.lock(reason="demande de l'agent")
        return {"ok": True, "locked": True}

    def vault_backup(destination: str) -> dict[str, Any]:
        target = vault.backup(destination)
        return {"ok": True, "path": str(target)}

    tools: list[Tool] = [
        FunctionTool(
            "vault_status",
            "État du coffre-fort : verrouillé ou non, nombre d'identifiants, temps restant avant verrouillage automatique.",
            object_schema({}),
            vault_status,
        ),
        FunctionTool(
            "vault_services",
            "Liste les services enregistrés (nom, identifiant, présence de 2FA). Aucun mot de passe n'est retourné.",
            object_schema({}),
            vault_services,
        ),
        FunctionTool(
            "vault_list_entries",
            "Recherche des identifiants par mot-clé (service, identifiant, URL, tag, note). Les mots de passe restent masqués.",
            object_schema(
                {
                    "query": param("string", "Filtre texte, vide pour tout lister", default=""),
                    "limit": param("integer", "Nombre maximum d'entrées", default=50, minimum=1, maximum=200),
                }
            ),
            vault_list_entries,
        ),
        FunctionTool(
            "vault_get_entry",
            "Retourne un champ précis d'une entrée. Les champs secrets sont masqués sauf demande explicite de l'utilisateur.",
            object_schema(
                {
                    "service": param("string", "Nom du service (domaine ou libellé)"),
                    "field_name": param(
                        "string",
                        "Champ à lire",
                        enum=["username", "password", "url", "notes", "totp_secret"],
                        default="password",
                    ),
                    "reveal": param(
                        "boolean",
                        "Révéler un secret en clair (à n'utiliser que sur demande explicite de l'utilisateur)",
                        default=False,
                    ),
                },
                required=["service"],
            ),
            vault_get_entry,
        ),
        FunctionTool(
            "vault_create_credential",
            "Génère un mot de passe fort et l'enregistre aussitôt dans le coffre pour un "
            "service ; la valeur n'est pas affichée, l'identifiant est prêt à l'emploi.",
            object_schema(
                {
                    "service": param("string", "Nom du service"),
                    "username": param("string", "Identifiant ou e-mail", default=""),
                    "password_length": param(
                        "integer", "Longueur du mot de passe généré", default=20, minimum=8, maximum=128
                    ),
                    "symbols": param("boolean", "Inclure des symboles", default=True),
                    "url": param("string", "URL de connexion", default=""),
                    "login_url": param(
                        "string", "Page de connexion (vérification après inscription)", default=""
                    ),
                    "notes": param("string", "Notes libres", default=""),
                    "tags": param("array", "Étiquettes", items=param("string", "Étiquette")),
                    "totp_secret": param("string", "Secret TOTP base32 pour la 2FA", default=""),
                },
                required=["service"],
            ),
            vault_create_credential,
            read_only=False,
            sensitive=True,
        ),
        FunctionTool(
            "vault_add_entry",
            "Enregistre un nouvel identifiant dans le coffre chiffré.",
            object_schema(
                {
                    "service": param("string", "Nom du service"),
                    "username": param("string", "Identifiant ou e-mail", default=""),
                    "password": param("string", "Mot de passe", default=""),
                    "url": param("string", "URL de connexion", default=""),
                    "login_url": param(
                        "string", "Page de connexion (vérification après inscription)", default=""
                    ),
                    "notes": param("string", "Notes libres", default=""),
                    "tags": param("array", "Étiquettes", items=param("string", "Étiquette")),
                    "totp_secret": param("string", "Secret TOTP base32 pour la 2FA", default=""),
                    "overwrite": param("boolean", "Remplacer une entrée existante", default=False),
                },
                required=["service"],
            ),
            vault_add_entry,
            read_only=False,
            sensitive=True,
        ),
        FunctionTool(
            "vault_update_entry",
            "Met à jour une entrée existante ; les champs omis restent inchangés.",
            object_schema(
                {
                    "service": param("string", "Nom du service"),
                    "username": param("string", "Identifiant actuel (filtre)", default=""),
                    "new_username": param("string", "Nouvel identifiant", default=""),
                    "password": param("string", "Nouveau mot de passe", default=""),
                    "url": param("string", "Nouvelle URL", default=""),
                    "login_url": param("string", "Nouvelle page de connexion", default=""),
                    "notes": param("string", "Nouvelles notes", default=""),
                    "tags": param("array", "Nouvelles étiquettes", items=param("string", "Étiquette")),
                },
                required=["service"],
            ),
            vault_update_entry,
            read_only=False,
            sensitive=True,
        ),
        FunctionTool(
            "vault_delete_entry",
            "Supprime définitivement une entrée du coffre.",
            object_schema(
                {
                    "service": param("string", "Nom du service"),
                    "username": param("string", "Identifiant (si plusieurs comptes)", default=""),
                },
                required=["service"],
            ),
            vault_delete_entry,
            read_only=False,
            dangerous=True,
        ),
        FunctionTool(
            "vault_generate_password",
            "Génère un mot de passe aléatoire fort.",
            object_schema(
                {
                    "password_length": param("integer", "Longueur souhaitée", default=20, minimum=8, maximum=128),
                    "symbols": param("boolean", "Inclure des symboles", default=True),
                }
            ),
            vault_generate_password,
        ),
        FunctionTool(
            "vault_totp",
            "Calcule le code 2FA courant (TOTP) d'un service.",
            object_schema(
                {
                    "service": param("string", "Nom du service"),
                    "username": param("string", "Identifiant (si plusieurs comptes)", default=""),
                },
                required=["service"],
            ),
            vault_totp,
        ),
        FunctionTool(
            "vault_lock",
            "Verrouille immédiatement le coffre-fort et efface la clé de la mémoire.",
            object_schema({}),
            vault_lock,
            read_only=False,
        ),
        FunctionTool(
            "vault_backup",
            "Copie le fichier chiffré du coffre vers une destination sous les racines "
            "autorisées (aucun fichier existant n'est écrasé).",
            object_schema({"destination": param("string", "Répertoire ou fichier de destination")}, required=["destination"]),
            vault_backup,
            read_only=False,
        ),
    ]
    return tools
