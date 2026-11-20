"""Politique de mise en page de l'interface, sans dépendance à Tk.

La fenêtre mesure ses besoins réels (barre latérale, contenu, hauteur de la
sidebar) puis délègue ici la décision compact/normal. Ce module reste pur :
il est testable sans écran et sert aussi à cadrer les fenêtres secondaires.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LayoutPlan",
    "fit_dialog_geometry",
    "plan_layout",
    "sidebar_needs_scroll",
    "wrap_length",
]

_MIN_WRAP = 160
_WRAP_PADDING = 32
_HEADING_GAP = 24


@dataclass(frozen=True)
class LayoutPlan:
    """Décisions de densité pour une taille de fenêtre donnée."""

    compact: bool
    wrap_length: int
    heading_wrap_length: int
    prompt_height: int
    nav_height: int
    nav_pady: int
    brand_pady: tuple[int, int]
    workspace_pady: tuple[int, int]
    workspace_label_pady: tuple[int, int]
    config_label_pady: tuple[int, int]
    sidebar_bottom_pady: int
    heading_pady: tuple[int, int]
    content_pady: tuple[int, int]
    welcome_center_pady: tuple[int, int]
    welcome_icon_visible: bool
    welcome_subtitle_visible: bool
    summary_visible: bool
    footer_visible: bool
    budget_details_visible: bool
    badge_visible: bool


def plan_layout(
    width: int,
    height: int,
    *,
    scale: float = 1.0,
    required_width: float = 0.0,
    required_height: float = 0.0,
    sidebar_required_height: float = 0.0,
    assistant_width: float = 0.0,
    heading_width: float = 0.0,
    heading_actions_width: float = 0.0,
) -> LayoutPlan:
    """Décide du mode compact à partir des besoins mesurés.

    ``width`` et ``height`` sont les dimensions physiques de la fenêtre ;
    ``required_*`` sont des dimensions logiques. Aucun seuil fixe de type
    « 1100×720 » n'est utilisé : si les mesures évoluent (police, libellés,
    contenu), le mode compact suit.
    """
    scale = scale if scale > 0 else 1.0
    logical_width = float(width) / scale
    logical_height = float(height) / scale
    compact = (
        logical_width < max(0.0, required_width)
        or logical_height < max(0.0, required_height)
        or logical_height < max(0.0, sidebar_required_height)
    )
    if compact:
        wrap = (
            max(_MIN_WRAP, int(assistant_width - _WRAP_PADDING))
            if assistant_width > 0
            else 0
        )
        heading_wrap = (
            wrap_length(heading_width, heading_actions_width + _HEADING_GAP)
            if heading_width > 0
            else 0
        )
        return LayoutPlan(
            compact=True,
            wrap_length=wrap,
            heading_wrap_length=heading_wrap,
            prompt_height=52,
            nav_height=30,
            nav_pady=0,
            brand_pady=(14, 0),
            workspace_pady=(12, 10),
            workspace_label_pady=(0, 4),
            config_label_pady=(10, 4),
            sidebar_bottom_pady=8,
            heading_pady=(0, 10),
            content_pady=(8, 10),
            welcome_center_pady=(8, 8),
            welcome_icon_visible=False,
            welcome_subtitle_visible=False,
            summary_visible=False,
            footer_visible=False,
            budget_details_visible=False,
            badge_visible=True,
        )
    return LayoutPlan(
        compact=False,
        wrap_length=0,
        heading_wrap_length=0,
        prompt_height=62,
        nav_height=38,
        nav_pady=2,
        brand_pady=(28, 0),
        workspace_pady=(28, 22),
        workspace_label_pady=(0, 8),
        config_label_pady=(24, 8),
        sidebar_bottom_pady=16,
        heading_pady=(6, 22),
        content_pady=(12, 20),
        welcome_center_pady=(22, 12),
        welcome_icon_visible=True,
        welcome_subtitle_visible=True,
        summary_visible=True,
        footer_visible=True,
        budget_details_visible=True,
        badge_visible=False,
    )


def wrap_length(available_width: float, reserved: float = 0.0) -> int:
    """Largeur de repli d'un texte, dérivée de la place réellement offerte.

    ``reserved`` couvre ce qui occupe déjà la ligne (boutons d'en-tête, marge
    de sécurité). Le plancher ``_MIN_WRAP`` garde le texte lisible sur une
    fenêtre extrêmement étroite ; le résultat suit sinon la largeur mesurée.
    """
    usable = float(available_width) - max(0.0, float(reserved))
    return max(_MIN_WRAP, int(usable))


def sidebar_needs_scroll(
    content_height: float,
    viewport_height: float,
    *,
    tolerance: float = 2.0,
) -> bool:
    """Vrai seulement si la sidebar ne tient pas dans la hauteur disponible."""
    return content_height > viewport_height + max(0.0, tolerance)


def fit_dialog_geometry(
    *,
    parent_width: int,
    parent_height: int,
    screen_width: int,
    screen_height: int,
    desired_width: int,
    desired_height: int,
    margin: int = 32,
) -> tuple[int, int, int, int]:
    """Cadre une fenêtre secondaire dans l'écran, sans dépasser le parent.

    Retourne ``(largeur, hauteur, x, y)``. La fenêtre reste centrée sur le
    parent quand la place le permet, et se replie sur l'écran sinon.
    """
    available_width = max(320, min(int(parent_width), int(screen_width)) - margin)
    available_height = max(240, min(int(parent_height), int(screen_height)) - margin)
    width = max(320, min(int(desired_width), available_width))
    height = max(240, min(int(desired_height), available_height))
    x = max(0, (max(0, int(parent_width)) - width) // 2)
    y = max(0, (max(0, int(parent_height)) - height) // 2)
    return width, height, x, y
