"""Contrat central des capacités des outils.

Les drapeaux `read_only`, `dangerous` et `sensitive` étaient déclarés famille
par famille : rien ne signalait un outil ajouté sans drapeau, et les valeurs
par défaut (`read_only=True`) faisaient passer un oubli pour une lecture sûre.
Ce module tient l'unique table de classification, et `build_agent` la compare
aux outils réellement construits avant de rendre l'agent utilisable.

Pour chaque outil :

* **effet** : `effect == "write"` ; l'outil modifie un état (fichier, coffre,
  navigateur, index). Une lecture (`"read"`) ne modifie rien d'observable ;
* **portée** : fichiers, coffre, web, navigateur, shell, index ou OCR,
  affichée avant une confirmation ;
* **réseau** : l'outil contacte un service distant ;
* **rejeu** : la lecture peut être rejouée par `core.resume`. La liste blanche
  de reprise est **dérivée** de ce champ, plus recopiée à la main — c'est ce qui
  avait laissé `fs_read` (nom disparu) dans l'ancienne liste ;
* **danger** : l'outil peut détruire ou exécuter du code arbitraire ; il exige
  une confirmation, et le mode autonome le refuse sans règle explicite ;
* **secret** : l'outil manipule des valeurs secrètes (mot de passe, jeton,
  cookie) ; son résultat et ses arguments ne doivent jamais atteindre le modèle
  ni la trace.

Un outil absent de `CAPABILITIES` est refusé au démarrage : mieux vaut un agent
qui ne démarre pas qu'un outil dont personne n'a décidé les droits.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from tools.base import Tool

__all__ = [
    "CAPABILITIES",
    "SCOPES",
    "ToolCapabilities",
    "ToolContractError",
    "assert_capabilities",
    "capabilities_for",
    "dangerous_tools",
    "describe_table",
    "replayable_tools",
    "validate_capabilities",
]

SCOPES = frozenset(
    {"files", "vault", "web", "browser", "shell", "index", "ocr"}
)


class ToolContractError(RuntimeError):
    """Un outil n'est pas classé, ou sa déclaration diverge du contrat."""


@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    """Capacités déclarées d'un outil, indépendamment de son implémentation."""

    effect: Literal["read", "write"]
    scope: str = "files"
    network: bool = False
    replay: bool = False
    dangerous: bool = False
    sensitive: bool = False

    @property
    def read_only(self) -> bool:
        return self.effect == "read"

    @property
    def writes(self) -> bool:
        return self.effect == "write"

    @property
    def replayable(self) -> bool:
        """Seul un rejeu déclaré, en lecture, sans danger ni secret est permis."""
        return (
            self.replay
            and self.effect == "read"
            and not self.dangerous
            and not self.sensitive
        )


def _read(
    *,
    scope: str = "files",
    network: bool = False,
    replay: bool = False,
    dangerous: bool = False,
    sensitive: bool = False,
) -> ToolCapabilities:
    return ToolCapabilities(
        "read",
        scope=scope,
        network=network,
        replay=replay,
        dangerous=dangerous,
        sensitive=sensitive,
    )


def _write(
    *,
    scope: str = "files",
    network: bool = False,
    dangerous: bool = False,
    sensitive: bool = False,
) -> ToolCapabilities:
    return ToolCapabilities(
        "write",
        scope=scope,
        network=network,
        dangerous=dangerous,
        sensitive=sensitive,
    )


#: Classification unique. Toute nouvelle famille d'outils doit s'y inscrire.
CAPABILITIES: Mapping[str, ToolCapabilities] = MappingProxyType(
    {
        # -- fichiers
        "fs_list": _read(replay=True),
        "fs_info": _read(replay=True),
        "fs_read_text": _read(replay=True),
        "fs_write_text": _write(),
        "fs_mkdir": _write(),
        "fs_copy": _write(),
        "fs_move": _write(),
        "fs_delete": _write(dangerous=True),
        "fs_rename_bulk": _write(),
        "fs_search": _read(replay=True),
        "fs_find_duplicates": _read(replay=True),
        "fs_organize": _write(),
        "fs_archive": _write(),
        "fs_extract": _write(),
        "fs_convert": _write(),
        "fs_disk_usage": _read(replay=True),
        "fs_hash": _read(replay=True),
        # -- coffre-fort
        "vault_status": _read(scope="vault", replay=True),
        "vault_services": _read(scope="vault", replay=True),
        "vault_list_entries": _read(scope="vault", replay=True),
        "vault_get_entry": _read(scope="vault", replay=True),
        "vault_create_credential": _write(scope="vault", sensitive=True),
        "vault_add_entry": _write(scope="vault", sensitive=True),
        "vault_update_entry": _write(scope="vault", sensitive=True),
        "vault_delete_entry": _write(scope="vault", dangerous=True),
        "vault_generate_password": _read(scope="vault", replay=True),
        "vault_totp": _read(scope="vault", replay=True),
        "vault_lock": _write(scope="vault"),
        "vault_backup": _write(scope="vault"),
        # -- index documentaire
        "doc_index_build": _write(scope="index"),
        "doc_index_search": _read(scope="index", replay=True),
        "doc_index_stats": _read(scope="index", replay=True),
        # -- OCR
        "ocr_image": _read(scope="ocr", replay=True),
        "ocr_status": _read(scope="ocr", replay=True),
        # -- shell
        "shell_run": _write(scope="shell", dangerous=True),
        "shell_status": _read(scope="shell", replay=True),
        # -- web
        "web_search": _read(scope="web", network=True, replay=True),
        "web_fetch": _read(scope="web", network=True, replay=True),
        "web_search_and_fetch": _read(scope="web", network=True, replay=True),
        "web_deep_research": _read(scope="web", network=True, replay=True),
        # -- navigateur
        "login_to_service": _write(scope="browser", network=True),
        "browser_fill_signup": _write(scope="browser", network=True),
        "fill_form": _write(scope="browser"),
        "click_element": _write(scope="browser"),
        "extract_cookies": _read(scope="browser", sensitive=True),
        "browser_open": _write(scope="browser", network=True),
        "browser_text": _read(scope="browser"),
        "browser_fetch_rendered": _read(scope="browser", network=True),
        "browser_extract_links": _read(scope="browser", network=True),
        "browser_screenshot": _read(scope="browser"),
        "browser_download": _write(scope="browser", network=True),
        "browser_status": _read(scope="browser"),
        "browser_close": _write(scope="browser"),
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


def replayable_tools() -> frozenset[str]:
    """Liste blanche de reprise, dérivée du manifeste (jamais recopiée)."""
    return frozenset(name for name, caps in CAPABILITIES.items() if caps.replayable)


def dangerous_tools() -> frozenset[str]:
    """Outils qui exigent une confirmation explicite."""
    return frozenset(name for name, caps in CAPABILITIES.items() if caps.dangerous)


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
        if bool(getattr(tool, "dangerous", False)) != capabilities.dangerous:
            gaps.append(f"danger={bool(tool.dangerous)} (contrat {capabilities.dangerous})")
        if bool(getattr(tool, "sensitive", False)) != capabilities.sensitive:
            gaps.append(
                f"secret={bool(tool.sensitive)} (contrat {capabilities.sensitive})"
            )
        if capabilities.scope not in SCOPES:
            gaps.append(f"portee inconnue '{capabilities.scope}'")
        if capabilities.replay and not capabilities.replayable:
            gaps.append("rejeu declare sur un outil a effet, dangereux ou secret")
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


def describe_table() -> str:
    """Tableau Markdown, pour la documentation et `python -m tools.capabilities`."""
    lines = [
        "| Outil | Effet | Portee | Reseau | Rejeu | Danger | Secret |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in sorted(CAPABILITIES):
        caps = CAPABILITIES[name]
        lines.append(
            f"| `{name}` | {caps.effect} | {caps.scope} | "
            f"{'oui' if caps.network else 'non'} | "
            f"{'oui' if caps.replayable else 'non'} | "
            f"{'oui' if caps.dangerous else 'non'} | "
            f"{'oui' if caps.sensitive else 'non'} |"
        )
    return "\n".join(lines)


def as_dict() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "effect": caps.effect,
            "scope": caps.scope,
            "network": caps.network,
            "replay": caps.replayable,
            "dangerous": caps.dangerous,
            "sensitive": caps.sensitive,
        }
        for name, caps in CAPABILITIES.items()
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--json" in args:
        print(json.dumps(as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(describe_table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
