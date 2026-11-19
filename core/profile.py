"""Profil de capacités par run : moindre privilège, réseau borné et journalisé.

Tous les outils partageaient les capacités du processus : une tâche qui devait
lire un fichier pouvait aussi ouvrir un navigateur ou lancer un shell. Un profil
choisit, **avant** la construction de l'agent, les familles d'outils exposées :

* `read-only` : fichiers/index/OCR en lecture, ni écriture, ni réseau, ni shell ;
* `files` : mêmes familles, écriture comprise ;
* `files-web` : + recherche et lecture web ;
* `full` : tout (comportement historique).

Le filtrage se fait sur le manifeste `tools.capabilities` : un outil absent du
profil n'est ni proposé au modèle ni exécutable (le registre répond
`unknown_tool`). Une liste blanche de domaines complète le profil réseauté : un
appel vers un hôte non autorisé est refusé et journalisé dans
`data/network.jsonl`.

Limite assumée : c'est une isolation **au niveau des outils**, pas un bac à
sable système. Un outil conservé garde les droits du processus.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from config import APP_ID, Settings
from tools.capabilities import CAPABILITIES, SCOPES

__all__ = [
    "PROFILES",
    "NetworkGuard",
    "RunProfile",
    "apply_profile",
    "build_network_guard",
    "extract_host",
    "network_log_path",
    "profile_for",
    "resolve_profile",
]

logger = logging.getLogger(f"{APP_ID}.profile")

#: Clés d'arguments qui portent une URL ; les autres outils réseautés (recherche)
#: n'ont pas d'hôte choisi par le modèle et restent soumis au seul profil.
_URL_KEYS = ("url", "uri", "href", "start_url", "page_url", "link")


@dataclass(frozen=True, slots=True)
class RunProfile:
    """Familles autorisées, écriture et réseau, pour une exécution."""

    name: str
    scopes: frozenset[str]
    writes: bool = False
    network: bool = False
    description: str = ""

    def allows(self, tool: str) -> bool:
        capabilities = CAPABILITIES.get(str(tool))
        if capabilities is None:
            return False
        if capabilities.scope not in self.scopes:
            return False
        if capabilities.writes and not self.writes:
            return False
        if capabilities.network and not self.network:
            return False
        return True


PROFILES: Mapping[str, RunProfile] = {
    "read-only": RunProfile(
        "read-only",
        frozenset({"files", "index", "ocr"}),
        writes=False,
        network=False,
        description="lecture de fichiers, index et OCR ; ni écriture, ni réseau, ni shell",
    ),
    "files": RunProfile(
        "files",
        frozenset({"files", "index", "ocr"}),
        writes=True,
        network=False,
        description="fichiers en lecture/écriture ; ni réseau, ni shell",
    ),
    "files-web": RunProfile(
        "files-web",
        frozenset({"files", "index", "ocr", "web"}),
        writes=True,
        network=True,
        description="fichiers et web ; ni navigateur, ni shell, ni coffre",
    ),
    "full": RunProfile(
        "full",
        SCOPES,
        writes=True,
        network=True,
        description="toutes les familles (comportement historique)",
    ),
}


def profile_for(name: str) -> RunProfile:
    """Profil nommé ; `full` si le nom est vide, `ValueError` s'il est inconnu."""
    key = str(name or "full").strip().lower()
    if not key:
        return PROFILES["full"]
    if key not in PROFILES:
        raise ValueError(
            f"profil inconnu '{name}' ; disponibles : {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[key]


def resolve_profile(settings: Settings, override: str | None = None) -> RunProfile:
    cfg = getattr(settings, "isolation", None)
    name = override or str(getattr(cfg, "profile", "full") or "full")
    return profile_for(name)


def apply_profile(tools: Iterable[Any], profile: RunProfile) -> list[Any]:
    """Ne garde que les outils que le profil autorise."""
    return [tool for tool in tools if profile.allows(str(getattr(tool, "name", "")))]


def extract_host(arguments: Mapping[str, Any]) -> str:
    """Premier hôte trouvé dans les arguments, en minuscules."""
    for key in _URL_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            continue
        host = (parsed.hostname or "").strip().lower()
        if host:
            return host
    return ""


def _domain_allowed(host: str, domains: Iterable[str]) -> bool:
    wanted = [str(item).strip().lower().lstrip(".") for item in domains if str(item).strip()]
    if not wanted:
        return True
    return any(host == domain or host.endswith("." + domain) for domain in wanted)


def network_log_path(settings: Settings) -> Path:
    return settings.paths.data_dir / "network.jsonl"


def _log_network(settings: Settings, record: dict[str, Any]) -> None:
    cfg = getattr(settings, "isolation", None)
    if cfg is not None and not getattr(cfg, "log_network", True):
        return
    path = network_log_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("sortie réseau non journalisée", exc_info=True)


@dataclass(slots=True)
class NetworkGuard:
    """Vérifie le profil et la liste blanche avant tout outil réseauté."""

    profile: RunProfile
    allowed_domains: list[str] = field(default_factory=list)
    settings: Settings | None = None

    def __post_init__(self) -> None:
        # Un garde construit sans liste explicite reprend la configuration :
        # oublier le paramètre ne doit pas élargir les domaines autorisés.
        if not self.allowed_domains and self.settings is not None:
            cfg = getattr(self.settings, "isolation", None)
            self.allowed_domains = list(getattr(cfg, "allowed_domains", []) or [])

    def __call__(self, tool: str, arguments: Mapping[str, Any]) -> tuple[bool, str]:
        capabilities = CAPABILITIES.get(str(tool))
        if capabilities is None or not capabilities.network:
            return True, ""
        host = extract_host(arguments)
        allowed = self.profile.network and (not host or _domain_allowed(host, self.allowed_domains))
        if not self.profile.network:
            reason = f"profil {self.profile.name} : reseau interdit"
        elif host and not allowed:
            reason = f"domaine non autorise : {host}"
        else:
            reason = ""
        if self.settings is not None:
            _log_network(
                self.settings,
                {
                    "at": datetime.now(tz=timezone.utc).isoformat(),
                    "tool": str(tool),
                    "host": host,
                    "profile": self.profile.name,
                    "allowed": allowed,
                    "reason": reason,
                },
            )
        return allowed, reason


def build_network_guard(
    settings: Settings, profile: RunProfile
) -> NetworkGuard | None:
    cfg = getattr(settings, "isolation", None)
    domains = list(getattr(cfg, "allowed_domains", []) or []) if cfg is not None else []
    log_network = bool(getattr(cfg, "log_network", True)) if cfg is not None else True
    if profile.network and not domains and not log_network:
        return None
    return NetworkGuard(profile=profile, allowed_domains=domains, settings=settings)
