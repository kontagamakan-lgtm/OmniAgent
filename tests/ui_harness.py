"""Construction partagée d'une vraie fenêtre OmniAgent pour les tests UI.

Le rendu des tests Tk est normalisé sur le cas du runner (100 %, 96 ppp) :
sans cela, une fenêtre ouverte en « 900x620 » mesurait 1125x775 sur un poste
réglé à 125 %, et les références ne valaient que pour la machine qui les avait
produites.
"""

from __future__ import annotations

import ctypes
import os
import time
import tkinter
from pathlib import Path
from types import SimpleNamespace

from support import make_settings

#: Drapeau qui active la normalisation, réservé aux captures de référence :
#: les autres tests gardent le rendu réel du poste.
STABLE_RENDERING_FLAG = "OMNIAGENT_UI_SNAPSHOTS"


def _enable_stable_rendering() -> None:
    """Aligne le rendu sur 1 px = 1 px et 96 ppp, quels que soient le poste et le DPI."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass

    original_init = tkinter.Tk.__init__

    def init_with_96_dpi(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.tk.call("tk", "scaling", 96 / 72)

    tkinter.Tk.__init__ = init_with_96_dpi
    try:
        import customtkinter as ctk
        from customtkinter.windows.widgets.scaling.scaling_tracker import (
            ScalingTracker,
        )
    except ImportError:  # pragma: no cover - sans customtkinter, pas de fenêtre
        return
    ScalingTracker.get_window_dpi_scaling = classmethod(lambda cls, window: 1.0)
    ctk.set_window_scaling(1.0)
    ctk.set_widget_scaling(1.0)


if os.environ.get(STABLE_RENDERING_FLAG) == "1":
    _enable_stable_rendering()


class UnusedLLM:
    supports_stream = False

    def complete(self, *_args, **_kwargs):
        raise AssertionError("aucun appel LLM dans les tests de géométrie")

    def close(self) -> None:
        pass


def settings_for_ui(root: Path, *, font_size: int = 13):
    settings = make_settings(root)
    settings.llm.backend = "ollama"
    settings.ui.font_size = font_size
    settings.budget.enabled = False
    settings.trace.enabled = False
    settings.memory.enabled = False
    settings.obsidian.enabled = False
    settings.evolution.enabled = False
    return settings


def make_window(settings):
    from core.agent_core import AgentCore
    from core.tool_registry import ToolRegistry
    from ui.app_gui import OmniAgentWindow

    registry = ToolRegistry([])
    agent = AgentCore(settings, UnusedLLM(), registry)
    bundle = SimpleNamespace(
        agent=agent,
        registry=registry,
        evolver=None,
        settings=settings,
    )
    return OmniAgentWindow(settings, bundle)


def grab_client(window, width: int, height: int):
    """Capture la fenêtre cliente elle-même, jamais la région d'écran.

    `ImageGrab` lit l'écran composé : une fenêtre transparente laisse voir le
desktop, et un autre programme posé dessus remplace le contenu. `PrintWindow`
demande à la fenêtre de se dessiner dans un tampon mémoire, ce qui donne
exactement ce que Tk a peint, à la taille demandée.
    """
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd = window.winfo_id()
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)
    user32.PrintWindow(hwnd, memory_dc, 2)  # PW_RENDERFULLCONTENT

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    header = BitmapInfoHeader()
    header.biSize = ctypes.sizeof(BitmapInfoHeader)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory_dc)
    user32.ReleaseDC(hwnd, window_dc)
    return Image.frombuffer(
        "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
    ).convert("RGB")


def settle(window: object, *, rounds: int = 3, delay: float = 0.08) -> None:
    for _ in range(rounds):
        window.update_idletasks()
        window.update()
        time.sleep(delay)
    window.update()
