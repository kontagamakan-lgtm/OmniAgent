"""Page de release : sections françaises stables et contenu GitHub.

Le corps de release était le « What's Changed » brut de GitHub : un lecteur non
technique n'y trouvait ni quoi installer, ni comment vérifier l'empreinte. Ces
tests figent l'ordre des sections et vérifient que le texte produit reste du
Markdown utilisable, sans dépendre du réseau.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_smoke"))

from write_release_notes import _notes_command, compose, pick_previous_tag  # noqa: E402


def test_la_page_commence_par_la_version_et_l_installateur() -> None:
    notes = compose("1.1.3", "## What's Changed\n* une PR")

    lines = notes.splitlines()
    assert lines[0] == "## OmniAgent 1.1.3"
    assert "`OmniAgent-1.1.3-Setup.exe`" in notes
    # Le dossier de données est celui de l'exe figé, pas celui des sources.
    assert "`%LOCALAPPDATA%\\OmniAgent`" in notes


def test_les_sections_sont_ordonnees_et_completes() -> None:
    notes = compose("1.1.3", "* une PR")

    positions = [
        notes.index("## OmniAgent 1.1.3"),
        notes.index("## Vérifier le téléchargement"),
        notes.index("## Changements"),
    ]
    assert positions == sorted(positions)
    assert "`SHA256SUMS.txt`" in notes
    assert "`BUILD-INFO.txt`" in notes


def test_le_contenu_github_est_conserve_tel_quel() -> None:
    generated = "## What's Changed\n* Casse le cycle d'import by @x in https://example/1"
    notes = compose("1.1.3", generated)

    assert generated in notes
    assert notes.endswith("\n")


def test_le_tag_precedent_est_le_plus_grand_numero_inferieur() -> None:
    """Le prédécesseur ne dépend ni de l'historique git ni d'un tag orphelin.

    La publication de 1.2.0 a échoué sur un clone superficiel : `git describe`
    ne trouvait pas le tag. La liste vient donc de l'API, et elle ne contient
    que les releases publiées : un tag resté sans release ne doit pas devenir
    le point de comparaison. Les versions plus récentes (republication d'un tag
    historique) et les tags non versionnés sont ignorés aussi.
    """
    tags = ["v1.0.4", "v1.1.0", "v1.1.3", "v1.2.0", "v1.3.0", "derniere"]
    assert pick_previous_tag("v1.2.0", tags) == "v1.1.3"
    assert pick_previous_tag("v1.1.0", tags) == "v1.0.4"
    assert pick_previous_tag("v1.0.4", tags) == ""


def test_un_tag_sans_release_n_est_jamais_le_precedent(monkeypatch) -> None:
    """Régression de la 1.2.1 : son tag 1.2.0 voisin n'avait pas de release.

    Comparée à tous les tags, la page annonçait « changements depuis 1.2.0 »,
    donc aucun des correctifs livrés. Seules les releases publiées comptent.
    """
    import write_release_notes as module

    monkeypatch.setattr(
        module, "_released_tags", lambda repository: ["v1.1.2", "v1.1.3", "v1.2.1"]
    )
    assert module.previous_tag("v1.2.1", "o/r") == "v1.1.3"


def test_les_versions_publiees_sont_lues_dans_les_releases(monkeypatch) -> None:
    """La source est `releases`, pas `tags` : c'est tout le correctif."""
    import subprocess

    import write_release_notes as module

    captured: dict[str, list[str]] = {}

    class _Done:
        returncode = 0
        stdout = "v1.1.3\n\n v1.2.1\n"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert module._released_tags("o/r") == ["v1.1.3", "v1.2.1"]
    assert "repos/o/r/releases?per_page=100" in captured["command"]


def test_une_version_sans_predecesseur_omet_le_tag_precedent() -> None:
    """`previous_tag_name` vide fait échouer l'API : la commande l'omet."""
    command = _notes_command("v1.0.0", "", "o/r")
    assert "previous_tag_name=" not in " ".join(command)
    assert _notes_command("v1.2.0", "v1.1.3", "o/r")[5:] == [
        "-f",
        "previous_tag_name=v1.1.3",
        "--jq",
        ".body",
    ]


def test_une_generation_vide_ne_laisse_pas_de_section_muette() -> None:
    notes = compose("1.1.3", "   \n")

    assert "Aucune pull request listée par GitHub." in notes
