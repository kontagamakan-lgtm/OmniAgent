"""Tests Tk opt-in de la fenêtre principale.

Ils instancient une vraie fenêtre CustomTkinter, ce que la suite ordinaire
évite pour rester exécutable sans écran. Activer avec :

    $env:OMNIAGENT_UI_TESTS = "1"
    python -m pytest tests/test_ui_responsive.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import tmpdir  # noqa: E402
from ui_harness import make_window, settings_for_ui, settle  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIAGENT_UI_TESTS") != "1",
    reason=(
        "tests Tk opt-in : définir OMNIAGENT_UI_TESTS=1 sur une session "
        "Windows avec affichage"
    ),
)


def _open_window(tmp: Path):
    window = make_window(settings_for_ui(tmp))
    settle(window)
    return window


def test_compact_900_keeps_sidebar_and_panels_reachable() -> None:
    with tmpdir("ui-responsive-min") as tmp:
        window = _open_window(tmp)
        try:
            window.geometry("900x620+0+0")
            settle(window)

            assert window._compact_layout
            assert window.summary_panel.winfo_ismapped() == 0
            assert window.footer.winfo_ismapped() == 0
            assert window.layout_badge.winfo_ismapped() == 1
            assert window.sidebar._parent_canvas.winfo_height() <= window.winfo_height()
            assert window.welcome_title.winfo_reqwidth() <= window.welcome_frame.winfo_width()
            assert window._sidebar_scroll_visible is False
            assert all(button.winfo_ismapped() for button in window._nav_buttons)
        finally:
            window._on_close()


def test_normal_1280_restores_every_panel_after_compact() -> None:
    with tmpdir("ui-responsive-normal") as tmp:
        window = _open_window(tmp)
        try:
            window.geometry("900x620+0+0")
            settle(window)
            assert window._compact_layout

            window.geometry("1280x860+0+0")
            settle(window)

            assert not window._compact_layout
            assert window.summary_panel.winfo_ismapped() == 1
            assert window.footer.winfo_ismapped() == 1
            assert window.welcome_icon.winfo_ismapped() == 1
            assert window.welcome_subtitle.winfo_ismapped() == 1
            assert window.layout_badge.winfo_ismapped() == 0
            assert window.budget_reset_label.winfo_ismapped() == 1
            assert int(window.welcome_title.cget("wraplength")) == 0
            assert window._sidebar_scroll_visible is False
        finally:
            window._on_close()


def test_compact_round_trip_is_stable() -> None:
    with tmpdir("ui-responsive-roundtrip") as tmp:
        window = _open_window(tmp)
        try:
            for _ in range(2):
                window.geometry("900x620+0+0")
                settle(window)
                assert window._compact_layout
                assert window.layout_badge.winfo_ismapped() == 1
                window.geometry("1280x860+0+0")
                settle(window)
                assert not window._compact_layout
                assert window.summary_panel.winfo_ismapped() == 1
        finally:
            window._on_close()


def test_long_status_and_budget_have_wrapping() -> None:
    with tmpdir("ui-responsive-long") as tmp:
        window = _open_window(tmp)
        try:
            assert int(window.status.cget("wraplength")) > 0
            assert int(window.budget_label.cget("wraplength")) > 0
            assert int(window.composer_note_label.cget("wraplength")) >= 0
        finally:
            window._on_close()


def test_press_motion_returns_to_rest_and_survives_fast_clicks() -> None:
    """Un appui puis relâchement retrouve la teinte d'origine, même en rafale."""
    with tmpdir("ui-press-motion") as tmp:
        window = _open_window(tmp)
        try:
            from ui.app_gui import _press_colors

            button = window._button(window, "Essai", lambda: None)
            button.place(x=0, y=0)
            settle(window, rounds=2, delay=0.05)
            rest = button.cget("fg_color")
            # CustomTkinter pose ses liaisons sur la toile interne du bouton.
            canvas = button._canvas

            canvas.event_generate("<ButtonPress-1>", when="now")
            settle(window, rounds=30, delay=0.02)
            assert button.cget("fg_color") == _press_colors(rest)[1]

            canvas.event_generate("<ButtonRelease-1>", when="now")
            settle(window, rounds=30, delay=0.02)
            assert button.cget("fg_color") == rest

            # Clics en rafale : chaque trajet interrompt le précédent. Sans
            # cela, deux minuteurs se chevauchent et le bouton reste sombre.
            for _ in range(3):
                canvas.event_generate("<ButtonPress-1>", when="now")
                settle(window, rounds=2, delay=0.02)
                canvas.event_generate("<ButtonRelease-1>", when="now")
                settle(window, rounds=2, delay=0.02)
            settle(window, rounds=40, delay=0.02)
            assert button.cget("fg_color") == rest
        finally:
            window._on_close()


def test_secondary_dialog_fits_the_minimum_window() -> None:
    with tmpdir("ui-responsive-dialog") as tmp:
        window = _open_window(tmp)
        try:
            window.geometry("900x620+0+0")
            settle(window)
            window._open_scheduler()
            settle(window)
            dialogs = [
                child
                for child in window.winfo_children()
                if child.winfo_class() == "Toplevel"
            ]
            assert dialogs, "la fenêtre du planificateur doit exister"
            dialog = dialogs[-1]
            try:
                assert dialog.winfo_width() <= window.winfo_width()
                assert dialog.winfo_height() <= window.winfo_height()
            finally:
                dialog.destroy()
        finally:
            window._on_close()


def test_large_font_gets_a_scrollable_sidebar() -> None:
    with tmpdir("ui-responsive-font") as tmp:
        window = make_window(settings_for_ui(tmp, font_size=28))
        try:
            window.geometry("900x620+0+0")
            settle(window)
            content = window._sidebar_content_height()
            viewport = window.sidebar._parent_canvas.winfo_height()

            assert window._compact_layout
            assert window._sidebar_scroll_visible == (content > viewport + 2)
            assert all(button.winfo_ismapped() for button in window._nav_buttons)
        finally:
            window._on_close()