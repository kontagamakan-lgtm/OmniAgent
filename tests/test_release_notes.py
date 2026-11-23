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

from write_release_notes import compose  # noqa: E402


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


def test_une_generation_vide_ne_laisse_pas_de_section_muette() -> None:
    notes = compose("1.1.3", "   \n")

    assert "Aucune pull request listée par GitHub." in notes
