"""La typographie de l'interface : un seul jeu de signes, pas d'icones detournees.

Une interface qui melange emoji, pictogrammes et symboles geometriques change
d'apparence d'une machine a l'autre (chaque systeme dessine ces signes a sa
facon) et ne garde aucune graisse commune. Ces tests tiennent deux regles :

* aucun pictogramme ni symbole geometrique dans les textes affiches : seuls la
  coche, le point median et le rond de statut restent admis, parce qu'ils sont
  dessines par la police du systeme et servent de ponctuation, pas d'icone ;
* aucun tiret cadratin, qui n'a pas sa place dans un libelle court ;
* un petit libelle reste lisible : les teintes de texte de l'interface et du
  theme atteignent le seuil AA (4,5:1) sur chaque fond ou elles s'affichent.
"""

from __future__ import annotations

import json
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


def _luminance(color: str) -> float:
    """Luminance relative WCAG d'une couleur `#RRGGBB` opaque."""
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.03928
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    """Rapport de contraste WCAG entre deux couleurs opaques."""
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


#: Seuil WCAG AA du texte normal. Un libelle de 10 px n'y echappe pas : le
#: seuil allege de 3:1 est reserve aux grands caracteres.
_AA_SMALL_TEXT = 4.5


def test_small_dark_labels_clear_aa_on_every_surface() -> None:
    """Chaque teinte de texte de la fenetre doit passer sur chaque fond.

    Un libelle de 10 px change de fond selon sa place (barre laterale, carte,
    surface elevee, fond de page) : c'est le fond le plus clair qui decide.
    """
    from ui.app_gui import (
        _UI_BG,
        _UI_FAINT,
        _UI_MUTED,
        _UI_SIDEBAR,
        _UI_SURFACE,
        _UI_SURFACE_2,
        _UI_TEXT,
    )

    surfaces = {
        "fond": _UI_BG,
        "barre laterale": _UI_SIDEBAR,
        "carte": _UI_SURFACE,
        "surface elevee": _UI_SURFACE_2,
    }
    for label, color in (
        ("faint", _UI_FAINT),
        ("muted", _UI_MUTED),
        ("texte", _UI_TEXT),
    ):
        for name, surface in surfaces.items():
            ratio = _contrast(color, surface)
            assert ratio >= _AA_SMALL_TEXT, f"{label} {color} sur {name}: {ratio:.2f}:1"


def test_theme_text_clears_aa_in_both_appearances() -> None:
    """Le theme partage doit tenir en clair comme en sombre.

    Les fenetres secondaires suivent l'apparence choisie : une teinte reglee
    pour le sombre seul retombe sous le seuil des que le theme clair revient.
    """
    theme = json.loads((ROOT / "assets" / "theme.json").read_text(encoding="utf-8"))
    pairs = (
        ("libelle / fond", "CTkLabel", "text_color", "CTk", "fg_color"),
        ("libelle / carte", "CTkLabel", "text_color", "CTkFrame", "fg_color"),
        ("champ / fond de champ", "CTkEntry", "text_color", "CTkEntry", "fg_color"),
        ("bouton / aplat", "CTkButton", "text_color", "CTkButton", "fg_color"),
    )
    for name, fg_widget, fg_key, bg_widget, bg_key in pairs:
        for index, appearance in ((0, "clair"), (1, "sombre")):
            ratio = _contrast(
                theme[fg_widget][fg_key][index], theme[bg_widget][bg_key][index]
            )
            assert ratio >= _AA_SMALL_TEXT, f"{name} ({appearance}): {ratio:.2f}:1"


def test_key_status_dots_stay_visible_in_both_appearances() -> None:
    """Une pastille est un aplat graphique : le seuil est 3:1, pas 4,5:1."""
    from ui.key_panel import STATUS_COLORS

    backgrounds = (("clair", "#FFFFFF"), ("sombre", "#151920"))
    for status, colors in STATUS_COLORS.items():
        for index, (appearance, background) in enumerate(backgrounds):
            ratio = _contrast(colors[index], background)
            assert ratio >= 3.0, f"{status} ({appearance}): {ratio:.2f}:1"
