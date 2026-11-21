"""Les pages légales existent, sont complètes et sont liées depuis le README.

Une page de confidentialité et des conditions d'utilisation peuvent disparaître
d'un dépôt sans que rien ne le signale : aucun test ne les lisait. Ces contrôles
tiennent les deux promesses minimales - les fichiers existent avec leurs
sections obligatoires, et le README y renvoie (une page que personne ne trouve
n'existe pas).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVACY = ROOT / "docs" / "PRIVACY.md"
TERMS = ROOT / "docs" / "TERMS.md"
README = ROOT / "README.md"


def _text(path: Path) -> str:
    assert path.is_file(), f"page absente : {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def test_privacy_page_covers_the_required_ground() -> None:
    text = _text(PRIVACY).lower()

    for section in ("données", "supprimer", "quitte la machine", "licences"):
        assert section in text, f"docs/PRIVACY.md : section « {section} » absente"


def test_terms_page_covers_the_required_ground() -> None:
    text = _text(TERMS).lower()

    for section in ("responsabilité", "usage acceptable", "garantie", "droit applicable"):
        assert section in text, f"docs/TERMS.md : section « {section} » absente"


def test_readme_links_both_pages() -> None:
    text = _text(README)

    for page in ("docs/PRIVACY.md", "docs/TERMS.md"):
        assert page in text, f"README.md ne renvoie pas vers {page}"


def test_pages_avoid_typographic_em_dashes() -> None:
    """Ces pages sont lues par des utilisateurs : pas de tiret cadratin."""
    for page in (PRIVACY, TERMS, README):
        assert "\u2014" not in _text(page), f"{page.name} contient un tiret cadratin"
