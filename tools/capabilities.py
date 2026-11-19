"""Contrat central des capacités des outils.

Les drapeaux `read_only`, `dangerous` et `sensitive` étaient déclarés famille
par famille : rien ne signalait un outil ajouté sans drapeau, et les valeurs
par défaut (`read_only=True`) faisaient passer un oubli pour une lecture sûre.
Ce module tient l'unique table de classification, et `build_agent` la compare
aux outils réellement construits avant de rendre l'agent utilisable.

Quatre capacités sont décrites pour chaque outil :

* **écriture** : `effect == "write"` ; l'outil modifie un état (fichier,
  coffre, navigateur, index). Un outil de lecture (`"read"`) ne modifie rien
  d'observable, ce qui autorise son rejeu lors d'une reprise de trace ;
* **danger** : l'outil peut détruire ou exécuter du code arbitraire ; il exige
  une confirmation, et le mode autonome le refuse sans règle explicite ;
* **secret** : l'outil manipule des valeurs secrètes (mot de passe, jeton,
  cookie) ; son résultat et ses arguments ne doivent jamais atteindre le
  modèle ni la trace.

Un outil absent de `CAPABILITIES` est refusé au démarrage : mieux vaut un agent
qui ne démarre pas qu'un outil dont personne n'a décidé les droits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from tools.base import Tool

__all__ = [
    "CAPABILITIES",
    "ToolCapabilities",
    "ToolContractError",
    "assert_capabilities",
    "capabilities_for",
    "validate_capabilities",
]


class ToolContractError(RuntimeError):
    """Un outil n'est pas classé, ou sa déclaration diverge du contrat."""


@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    """Capacités déclarées d'un outil, indépendamment de son implémentation."""

    effect: Literal["read", "write"]
    dangerous: bool = False
    sensitive: bool = False

    @property
    def read_only(self) -> bool:
        return self.effect == "read"

    @property
    def writes(self) -> bool:
        return self.effect == "write"


def _read(*, dangerous: bool = False, sensitive: bool = False) -> ToolCapabilities:
    return ToolCapabilities("read", dangerous=dangerous, sensitive=sensitive)


def _write(*, dangerous: bool = False, sensitive: bool = False) -> ToolCapabilities:
    return ToolCapabilities("write", dangerous=dangerous, sensitive=sensitive)


#: Classification unique. Toute nouvelle famille d'outils doit s'y inscrire.
CAPABILITIES: Mapping[str, ToolCapabilities] = MappingProxyType(
    {
        # -- fichiers
        "fs_list": _read(),
        "fs_info": _read(),
        "fs_read_text": _read(),
        "fs_write_text": _write(),
        "fs_mkdir": _write(),
        "fs_copy": _write(),
        "fs_move": _write(),
        "fs_delete": _write(dangerous=True),
        "fs_rename_bulk": _write(),
        "fs_search": _read(),
        "fs_find_duplicates": _read(),
        "fs_organize": _write(),
        "fs_archive": _write(),
        "fs_extract": _write(),
        "fs_convert": _write(),
        "fs_disk_usage": _read(),
        "fs_hash": _read(),
        # -- coffre-fort
        "vault_status": _read(),
        "vault_services": _read(),
        "vault_list_entries": _read(),
        "vault_get_entry": _read(),
        "vault_create_credential": _write(sensitive=True),
        "vault_add_entry": _write(sensitive=True),
        "vault_update_entry": _write(sensitive=True),
        "vault_delete_entry": _write(dangerous=True),
        "vault_generate_password": _read(),
        "vault_totp": _read(),
        "vault_lock": _write(),
        "vault_backup": _write(),
        # -- index documentaire
        "doc_index_build": _write(),
        "doc_index_search": _read(),
        "doc_index_stats": _read(),
        # -- OCR
        "ocr_image": _read(),
        "ocr_status": _read(),
        # -- shell
        "shell_run": _write(dangerous=True),
        "shell_status": _read(),
        # -- web
        "web_search": _read(),
        "web_fetch": _read(),
        "web_search_and_fetch": _read(),
        "web_deep_research": _read(),
        # -- navigateur
        "login_to_service": _write(),
        "browser_fill_signup": _write(),
        "fill_form": _write(),
        "click_element": _write(),
        "extract_cookies": _read(sensitive=True),
        "browser_open": _write(),
        "browser_text": _read(),
        "browser_fetch_rendered": _read(),
        "browser_extract_links": _read(),
        "browser_screenshot": _read(),
        "browser_download": _write(),
        "browser_status": _read(),
        "browser_close": _write(),
    }
)


def capabilities_for(name: str) -> ToolCapabilities:
    """Capacités déclarées d'un outil ; lève si l'outil n'est pas classé."""
    capabilities = CAPABILITIES.get(str(name))
    if capabilities is None:
        raise ToolContractError(
            f"outil non classé : '{name}' ; déclarez-le dans tools/capabilities.py"
        )
    return capabilities


def validate_capabilities(tools: Iterable[Tool]) -> list[str]:
    """Compare les outils réels au contrat ; retourne les écarts lisibles.

    Une liste vide signifie que chaque outil construit est classé et que ses
    drapeaux correspondent exactement à sa déclaration.
    """
    problems: list[str] = []
    for tool in tools:
        capabilities = CAPABILITIES.get(tool.name)
        if capabilities is None:
            problems.append(
                f"outil non classé : '{tool.name}' ; "
                "déclarez-le dans tools/capabilities.py"
            )
            continue
        gaps: list[str] = []
        if bool(tool.read_only) != capabilities.read_only:
            gaps.append(
                f"lecture={bool(tool.read_only)} (contrat {capabilities.effect})"
            )
        if bool(tool.dangerous) != capabilities.dangerous:
            gaps.append(f"danger={bool(tool.dangerous)} (contrat {capabilities.dangerous})")
        if bool(tool.sensitive) != capabilities.sensitive:
            gaps.append(
                f"secret={bool(tool.sensitive)} (contrat {capabilities.sensitive})"
            )
        if gaps:
            problems.append(
                f"outil '{tool.name}' : drapeaux " + ", ".join(gaps)
            )
    return problems


def assert_capabilities(tools: Iterable[Tool]) -> None:
    """Blocage au démarrage : aucune divergence de contrat n'est tolérée."""
    problems = validate_capabilities(tools)
    if problems:
        raise ToolContractError(
            "contrat des capacités invalide :\n- " + "\n- ".join(problems)
        )
