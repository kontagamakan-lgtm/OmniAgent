"""La typographie de l'interface : un seul jeu de signes, pas d'icones detournees.

Une interface qui melange emoji, pictogrammes et symboles geometriques change
d'apparence d'une machine a l'autre (chaque systeme dessine ces signes a sa
facon) et ne garde aucune graisse commune. Ces tests tiennent deux regles :

* aucun pictogramme ni symbole geometrique dans les textes affiches : seuls la
  coche, le point median et le rond de statut restent admis, parce qu'ils sont
  dessines par la police du systeme et servent de ponctuation, pas d'icone ;
* aucun tiret cadratin, qui n'a pas sa place dans un libelle court.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"

#: Ponctuation conservee : coche, point median, rond de statut.
_ALLOWED = {"\u2713", "\u00b7", "\u25cf"}

#: Plages emoji, pictogrammes, dingbats et formes geometriques.
_ICON_RANGES = (
    (0x2190, 0x21FF),  # fleches
    (0x2300, 0x23FF),  # signes techniques
    (0x25A0, 0x25FF),  # formes geometriques
    (0x2600, 0x27BF),  # divers symboles et dingbats
    (0x2B00, 0x2BFF),  # fleches et formes supplementaires
    (0x1F300, 0x1FAFF),  # emoji
    (0xFE0F, 0xFE0F),  # selecteur de variante
)


def _modules() -> list[Path]:
    return sorted(UI.glob("*.py"))


def _is_icon(char: str) -> bool:
    return any(low <= ord(char) <= high for low, high in _ICON_RANGES)


def test_no_pictogram_in_interface_text() -> None:
    offenders: list[str] = []
    for path in _modules():
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for char in line:
                if char in _ALLOWED or not _is_icon(char):
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{index} U+{ord(char):04X}")

    assert not offenders, "pictogramme dans l'interface : " + ", ".join(offenders)


def test_no_em_dash_in_interface_text() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{index}"
        for path in _modules()
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "\u2014" in line
    ]

    assert not offenders, "tiret cadratin dans l'interface : " + ", ".join(offenders)


def test_navigation_has_no_glyph_slot() -> None:
    """La navigation ne reprend pas l'habitude de prefixer chaque libelle."""
    source = (UI / "app_gui.py").read_text(encoding="utf-8")

    assert "glyph=" not in source, "un emplacement d'icone subsiste dans la navigation"
