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
import re
import subprocess
import sys
from pathlib import Path

__all__ = ["compose", "pick_previous_tag", "previous_tag"]

#: Section stable : elle ne dépend pas du contenu de la version.
_INTRO = (
    "Installeur Windows : `OmniAgent-{version}-Setup.exe` (Windows 10/11, sans "
    "droits administrateur). Les données personnelles restent dans "
    "`%LOCALAPPDATA%\\OmniAgent` pendant une mise à jour."
)
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
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


def _version_key(tag: str) -> tuple[int, ...] | None:
    """Clé de tri d'un tag `vX.Y.Z` ; `None` si le tag n'en est pas un."""
    match = _TAG_RE.match(tag.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def pick_previous_tag(tag: str, tags: list[str]) -> str:
    """Plus grand tag de version strictement inférieur à `tag`.

    Le workflow publie une version après l'autre : le prédécesseur est celui
    dont le numéro est immédiatement inférieur. Les tags qui ne sont pas des
    versions (`derniere`, essais datés) ne peuvent pas servir de point de
    comparaison à `releases/generate-notes` et sont ignorés.
    """
    current = _version_key(tag)
    if current is None:
        return ""
    best: tuple[int, ...] | None = None
    best_tag = ""
    for candidate in tags:
        key = _version_key(candidate)
        if key is None or key >= current:
            continue
        if best is None or key > best:
            best, best_tag = key, candidate.strip()
    return best_tag


def _released_tags(repository: str) -> list[str]:
    """Tags ayant une release publiée (`gh` est déjà authentifié)."""
    completed = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/releases?per_page=100",
            "--paginate",
            "--jq",
            ".[].tag_name",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def previous_tag(tag: str, repository: str = "") -> str:
    """Tag publié avant `tag` (garde-fou : sans lui, GitHub liste tout).

    La comparaison porte sur les **releases publiées**, jamais sur les tags
    seuls : la 1.2.0 a laissé un tag sans release, et la page de la 1.2.1
    annonçait alors « changements depuis 1.2.0 », donc aucun des correctifs
    qu'elle apportait. Un tag orphelin ne peut plus fausser la page.

    `git describe` sert quand aucun dépôt n'est donné (usage hors ligne) : il
    donne l'ancêtre réel. Il est inutilisable dans le workflow de publication,
    où `actions/checkout` ne récupère que le commit du tag (clone superficiel) :
    `1.2.0` y a échoué en « Not a valid object name ». Vide quand aucune
    version n'a été publiée avant : `releases/generate-notes` liste alors
    tout, ce qui reste vrai.
    """
    if repository:
        return pick_previous_tag(tag, _released_tags(repository))
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
    if completed.returncode != 0:
        return ""
    return previous


def _notes_command(tag: str, previous: str, repository: str) -> list[str]:
    """Commande `gh api` de génération des notes."""
    command = [
        "gh",
        "api",
        f"repos/{repository}/releases/generate-notes",
        "-f",
        f"tag_name={tag}",
    ]
    # Un `previous_tag_name` vide fait échouer l'API : on l'omet, GitHub liste
    # alors tout l'historique.
    if previous:
        command += ["-f", f"previous_tag_name={previous}"]
    return command + ["--jq", ".body"]


def _generated_notes(tag: str, previous: str, repository: str) -> str:
    completed = subprocess.run(
        _notes_command(tag, previous, repository),
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
        generated = _generated_notes(
            args.tag, previous_tag(args.tag, repository), repository
        )
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
