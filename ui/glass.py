"""Effet « verre » de la fenêtre principale, quand Windows sait le peindre.

Windows 10 (à partir de 1803) et Windows 11 savent flouter ce qui se trouve
**derrière** une fenêtre. L'effet est demandé à DWM, qui le compose lui-même :
Tk ne sait pas flouter, et simuler un flou en peignant des pixels coûterait une
capture de bureau par image.

Tout est facultatif et silencieux : hors Windows, sur un Windows trop ancien ou
quand DWM refuse, la fonction ne fait rien et la fenêtre reste opaque. Un
`--check` en CI ou un test ne doit jamais échouer à cause d'un effet visuel.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

__all__ = ["apply_glass_backdrop", "glass_supported"]

logger = logging.getLogger(__name__)

#: `SetWindowCompositionAttribute`, non documenté, présent depuis Windows 7.
_ACCENT_ENABLE_BLURBEHIND = 3
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
_WCA_ACCENT_POLICY = 19


def glass_supported() -> bool:
    """Vrai seulement sur un Windows qui expose l'API d'accentuation."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return hasattr(ctypes.windll, "user32") and hasattr(ctypes.windll.user32, "SetWindowCompositionAttribute")
    except Exception:  # noqa: BLE001 - absence d'API = pas d'effet
        return False


def apply_glass_backdrop(window: Any, *, acrylic: bool = True, tint: int = 0) -> bool:
    """Applique un fond flouté à `window` (Tk). Retourne True si DWM a accepté."""
    if not glass_supported():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WindowCompositionAttributeData(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        # `winfo_id()` rend la fenêtre enfant ; DWM attend la fenêtre de premier
        # niveau, d'où le GetParent (sans effet si c'en est déjà une).
        hwnd = int(window.winfo_id())
        parent = ctypes.windll.user32.GetParent(hwnd)
        target = parent or hwnd

        # Teinte nulle par défaut : sans elle, DWM ne floute que ce qui est
        # **derrière** la fenêtre. Une teinte, même à alpha faible, s'applique à
        # toute la surface et assombrit chaque pixel de la fenêtre - mesuré en
        # CI : 16/255 d'écart uniforme sur les quatre références de snapshots.
        policy = AccentPolicy(
            _ACCENT_ENABLE_ACRYLICBLURBEHIND if acrylic else _ACCENT_ENABLE_BLURBEHIND,
            0,
            max(0, min(0xFFFFFFFF, int(tint))),
            0,
        )
        data = WindowCompositionAttributeData(
            _WCA_ACCENT_POLICY,
            ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
            ctypes.sizeof(policy),
        )
        setter = ctypes.windll.user32.SetWindowCompositionAttribute
        setter.argtypes = [wintypes.HWND, ctypes.POINTER(WindowCompositionAttributeData)]
        setter.restype = wintypes.BOOL
        if not setter(target, ctypes.byref(data)):
            logger.debug("DWM a refusé l'effet de verre sur %s", hwnd)
            return False
        return True
    except Exception:  # noqa: BLE001 - un effet visuel ne casse jamais l'interface
        logger.debug("effet de verre indisponible", exc_info=True)
        return False
