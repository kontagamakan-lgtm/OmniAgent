"""Raccourci global d'arrêt d'urgence (Windows).

Le raccourci lié à la fenêtre ne sert que lorsque Tk a le focus. Une session
autonome tourne souvent avec une autre application au premier plan : il faut
`RegisterHotKey`, qui capture la combinaison au niveau du système.

Le thread dédié possède sa propre boucle de messages Windows. Le callback est
appelé **depuis ce thread** : il ne doit pas toucher Tk. L'interface y dépose
seulement un signal dans une file, vidée par `_pump`.

La combinaison est Ctrl+Alt+S. Si une autre application l'a déjà prise,
`start()` retourne faux avec une raison ; l'application doit alors se rabattre
sur le bouton et le raccourci de fenêtre, pas échouer au démarrage.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections.abc import Callable

from config import APP_ID

__all__ = ["HOTKEY_LABEL", "HotkeyListener"]

logger = logging.getLogger(f"{APP_ID}.hotkey")

HOTKEY_LABEL = "Ctrl+Alt+S"
_HOTKEY_ID = 0xB17
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_VK_S = 0x53
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    """Copie de `MSG` : le champ `message` n'est pas au debut de la structure."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt", _POINT),
        ("lPrivate", ctypes.c_uint),
    ]


class HotkeyListener:
    """Enregistre Ctrl+Alt+S globalement et appelle `callback` à la pression."""

    def __init__(self) -> None:
        self._callback: Callable[[], None] | None = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.error = ""
        self._registered = False

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(self._registered and thread is not None and thread.is_alive())

    def _register_native(self) -> bool:
        """Réserve la combinaison ; séparé pour être simulable en test."""
        user32 = ctypes.windll.user32
        return bool(
            user32.RegisterHotKey(
                None,
                _HOTKEY_ID,
                _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT,
                _VK_S,
            )
        )

    def _unregister_native(self) -> None:
        try:
            ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
        except Exception:  # noqa: BLE001 - libération best effort
            logger.debug("désenregistrement du raccourci impossible", exc_info=True)

    def _handle_hotkey(self) -> None:
        """Appelé sur WM_HOTKEY ; isole les erreurs du callback."""
        callback = self._callback
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - un callback ne tue pas le thread
            logger.exception("raccourci global : callback en échec")

    def start(self, callback: Callable[[], None]) -> bool:
        """Démarre l'écoute ; faux si la plateforme ou une autre app refuse."""
        if os.name != "nt":
            self.error = "raccourci global : Windows uniquement"
            return False
        with self._lock:
            if self.running:
                self._callback = callback
                return True
            self._callback = callback
            self.error = ""
            self._registered = False
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._loop, name="omniagent-hotkey", daemon=True
            )
            self._thread.start()
            self._ready.wait(3.0)
            if not self.running:
                if not self.error:
                    self.error = "raccourci global indisponible"
                return False
            return True

    def _loop(self) -> None:
        try:
            if not self._register_native():
                self.error = (
                    f"{HOTKEY_LABEL} déjà utilisé par une autre application"
                )
                return
            self._registered = True
            self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            self._ready.set()
            user32 = ctypes.windll.user32
            message = _MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if int(message.message) == _WM_HOTKEY:
                    self._handle_hotkey()
        except Exception as exc:  # noqa: BLE001 - le raccourci ne casse pas l'app
            self.error = f"raccourci global en échec : {exc}"
            logger.exception("raccourci global en échec")
        finally:
            if self._registered:
                self._unregister_native()
                self._registered = False
            self._ready.set()

    def stop(self) -> None:
        """Retire le raccourci et termine le thread."""
        with self._lock:
            thread = self._thread
            self._thread = None
            self._callback = None
            thread_id = self._thread_id
            self._thread_id = 0
        if thread is None:
            return
        if os.name == "nt" and thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                logger.debug("arrêt du raccourci impossible", exc_info=True)
        thread.join(timeout=2.0)
        self._registered = False
