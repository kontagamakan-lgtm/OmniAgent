"""Politique responsive de l'interface, testée sans Tk.

Ces tests verrouillent les décisions qui étaient auparavant validées à la main
par des captures : mode compact, largeur de retour à la ligne, hauteurs de
navigation et cadrage des fenêtres secondaires.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import run  # noqa: E402

from ui.layout import (  # noqa: E402
    fit_dialog_geometry,
    plan_layout,
    sidebar_needs_scroll,
    wrap_length,
)


def test_normal_layout_uses_measured_requirements() -> None:
    plan = plan_layout(
        1280,
        860,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
        assistant_width=670,
    )

    assert not plan.compact
    assert plan.wrap_length == 0
    assert plan.prompt_height == 62
    assert plan.summary_visible and plan.footer_visible
    assert plan.budget_details_visible and plan.welcome_icon_visible
    assert not plan.badge_visible


def test_compact_layout_follows_width_requirement() -> None:
    plan = plan_layout(
        900,
        620,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
        assistant_width=290,
    )

    assert plan.compact
    assert 160 <= plan.wrap_length < 290
    assert plan.prompt_height == 52
    assert plan.nav_height == 30 and plan.nav_pady == 0
    assert not plan.summary_visible and not plan.footer_visible
    assert not plan.budget_details_visible
    assert not plan.welcome_icon_visible
    assert plan.badge_visible


def test_compact_layout_follows_sidebar_height() -> None:
    plan = plan_layout(
        1280,
        620,
        required_width=1006,
        required_height=500,
        sidebar_required_height=722,
        assistant_width=670,
    )

    assert plan.compact, "une sidebar trop haute doit déclencher le compact"


def test_heading_wrap_follows_available_width() -> None:
    """L'en-tête se replie selon sa largeur mesurée, pas sur un seuil fixe."""
    narrow = plan_layout(
        900,
        620,
        required_width=1006,
        required_height=722,
        assistant_width=290,
        heading_width=800,
        heading_actions_width=286,
    )
    wide = plan_layout(
        900,
        620,
        required_width=1006,
        required_height=722,
        assistant_width=290,
        heading_width=1000,
        heading_actions_width=286,
    )

    assert narrow.compact and wide.compact
    assert narrow.heading_wrap_length == 490
    assert wide.heading_wrap_length == 690
    assert wide.heading_wrap_length > narrow.heading_wrap_length
    assert narrow.heading_wrap_length != 400


def test_heading_wrap_is_off_in_normal_mode() -> None:
    plan = plan_layout(
        1280,
        860,
        required_width=1006,
        required_height=722,
        heading_width=1000,
        heading_actions_width=286,
    )

    assert not plan.compact
    assert plan.heading_wrap_length == 0


def test_wrap_length_is_derived_from_available_width() -> None:
    assert wrap_length(900, 300) == 600
    assert wrap_length(600, 300) == 300
    assert wrap_length(400, 300) == 160, "plancher de lisibilité seulement"


def test_boundaries_are_measured_not_hardcoded() -> None:
    normal = plan_layout(
        1006,
        722,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
    )
    narrow = plan_layout(
        1005,
        722,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
    )
    short = plan_layout(
        1006,
        721,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
    )

    assert not normal.compact
    assert narrow.compact
    assert short.compact


def test_scale_is_applied_to_physical_dimensions() -> None:
    compact = plan_layout(
        1125,
        775,
        scale=1.25,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
        assistant_width=290,
    )
    normal = plan_layout(
        1600,
        1055,
        scale=1.25,
        required_width=1006,
        required_height=722,
        sidebar_required_height=722,
        assistant_width=670,
    )

    assert compact.compact
    assert not normal.compact


def test_large_font_requirements_force_compact() -> None:
    plan = plan_layout(
        1280,
        860,
        required_width=1500,
        required_height=900,
        sidebar_required_height=900,
        assistant_width=670,
    )

    assert plan.compact


def test_zero_requirements_do_not_force_compact() -> None:
    assert not plan_layout(900, 620).compact


def test_small_assistant_keeps_a_usable_wrap() -> None:
    plan = plan_layout(
        900,
        620,
        required_width=1006,
        required_height=722,
        assistant_width=180,
    )

    assert plan.wrap_length == 160


def test_sidebar_scroll_only_when_content_overflows() -> None:
    assert not sidebar_needs_scroll(696, 775)
    assert not sidebar_needs_scroll(775, 775)
    assert sidebar_needs_scroll(1110, 775)
    assert sidebar_needs_scroll(780, 775)


def test_dialog_geometry_is_clamped_to_parent_and_screen() -> None:
    width, height, x, y = fit_dialog_geometry(
        parent_width=900,
        parent_height=620,
        screen_width=1920,
        screen_height=1080,
        desired_width=900,
        desired_height=720,
    )

    assert width == 868 and height == 588
    assert x == 16 and y == 16
    assert width <= 900 and height <= 620


def test_dialog_geometry_keeps_desired_size_when_it_fits() -> None:
    width, height, x, y = fit_dialog_geometry(
        parent_width=1280,
        parent_height=860,
        screen_width=1920,
        screen_height=1080,
        desired_width=760,
        desired_height=640,
    )

    assert (width, height) == (760, 640)
    assert (x, y) == (260, 110)


class _LongText:
    """Widget texte minimal avec index/delete, pour la rétention visuelle."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.state = "normal"

    def configure(self, **kwargs) -> None:
        self.state = kwargs.get("state", self.state)

    def index(self, _position: str) -> str:
        return f"{len(self.lines)}.0"

    def insert(self, index: str, text: str) -> None:
        if index == "end":
            self.lines.extend(text.rstrip("\n").splitlines())
        elif index == "1.0":
            self.lines.insert(0, text.rstrip("\n"))

    def delete(self, start: str, end: str) -> None:
        first = int(start.split(".", 1)[0]) - 1
        last = int(end.split(".", 1)[0]) - 1
        del self.lines[first:last]

    def see(self, _index: str) -> None:
        pass

    def get(self, _start: str, _end: str) -> str:
        return "\n".join(self.lines)


def test_transcript_buffer_caps_a_very_long_session() -> None:
    from ui.app_gui import TranscriptBuffer

    widget = _LongText([f"ligne {index}" for index in range(500)])
    buffer = TranscriptBuffer(widget, max_lines=100, trim_chunk=10)

    buffer.write("nouvelle ligne")

    assert len(widget.lines) <= 102, len(widget.lines)
    assert widget.lines[0].startswith("• historique ancien tronqué")
    assert widget.state == "disabled"


if __name__ == "__main__":
    sys.exit(run(globals()))
