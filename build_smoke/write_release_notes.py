"""Compose le corps français d'une release GitHub.

GitHub ne sait produire que son « What's Changed » en anglais. Une page de
release doit d'abord dire quoi installer, comment vérifier l'empreinte et ce
qui a changé ; seule la dernière section vient de `gh api
releases/generate-notes`.

Le script est appelé par `.github/workflows/release.yml` :

    python build_smoke/write_release_notes.py --tag v1.1.3 \\
        --version 1.1.3 --output release-notes.md
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

__all__ = ["compose", "previous_tag"]

#: Section stable : elle ne dépend pas du contenu de la version.
_INTRO = (
    "Installeur Windows : `OmniAgent-{version}-Setup.exe` (Windows 10/11, sans "
    "droits administrateur). Les données personnelles restent dans "
    "`%LOCALAPPDATA%\\OmniAgent` pendant une mise à jour."
)
_VERIFY = (
    "`SHA256SUMS.txt` porte l'empreinte de chaque fichier publié ; "
    "`BUILD-INFO.txt` donne la version de Python, celle de PyInstaller et le "
    "commit construit. Comparez l'empreinte avant d'exécuter l'installateur."
)


def compose(version: str, generated: str) -> str:
    """Assemble la page de release ; `generated` vient de GitHub."""
    changes = generated.strip() or "Aucune pull request listée par GitHub."
    return "\n".join(
        [
            f"## OmniAgent {version}",
            "",
            _INTRO.format(version=version),
            "",
            "## Vérifier le téléchargement",
            "",
            _VERIFY,
            "",
            "## Changements",
            "",
            changes,
            "",
        ]
    )


def previous_tag(tag: str) -> str:
    """Tag publié avant `tag` (garde-fou : sans lui, GitHub liste tout)."""
    completed = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", f"{tag}^"],
        capture_output=True,
        text=True,
        # `gh` et `git` écrivent en UTF-8 ; la locale Windows décode en cp1252
        # et transformerait « agrégé » en « agrÃ©gÃ© » dans la page publiée.
        encoding="utf-8",
        check=False,
    )
    previous = completed.stdout.strip()
    if completed.returncode != 0 or not previous:
        raise SystemExit(
            f"tag précédent introuvable pour {tag} : "
            f"{completed.stderr.strip() or 'aucune sortie'}"
        )
    return previous


def _generated_notes(tag: str, previous: str, repository: str) -> str:
    completed = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/releases/generate-notes",
            "-f",
            f"tag_name={tag}",
            "-f",
            f"previous_tag_name={previous}",
            "--jq",
            ".body",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "génération des notes impossible : "
            f"{completed.stderr.strip() or 'aucune sortie'}"
        )
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="tag publié (ex. v1.1.3)")
    parser.add_argument("--version", required=True, help="APP_VERSION (ex. 1.1.3)")
    parser.add_argument("--output", required=True, help="fichier Markdown à écrire")
    parser.add_argument(
        "--repository",
        default="",
        help="propriétaire/dépôt (défaut : GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--generated",
        default="",
        help="notes GitHub déjà générées (sinon elles sont demandées à l'API)",
    )
    args = parser.parse_args(argv)

    generated = args.generated
    if not generated:
        repository = args.repository or _env("GITHUB_REPOSITORY")
        generated = _generated_notes(args.tag, previous_tag(args.tag), repository)
    target = Path(args.output)
    target.write_text(compose(args.version, generated), encoding="utf-8")
    print(f"Notes de release écrites : {target}")
    return 0


def _env(name: str) -> str:
    import os

    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"variable {name} absente : impossible de nommer le dépôt")
    return value


if __name__ == "__main__":
    sys.exit(main())
