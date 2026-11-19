"""Notification Windows et raccourci global, sans session interactive."""

from __future__ import annotations

import base64
import ctypes
import os
import queue
import threading
from types import SimpleNamespace

from support import tmpdir  # noqa: I001

import core.notify as notify_module
from core.notify import build_toast_script, notify_windows
from core.schedule import Schedule, run_scheduled
from core.stopfile import stop_path
from ui.hotkey import HotkeyListener


def test_toast_script_escapes_user_text() -> None:
    script = build_toast_script("Titre <x>", "Budget < 5 & fin")

    assert "&lt;x&gt;" in script
    assert "&amp;" in script
    assert "<toast>" in script


def test_notification_passes_an_encoded_script_to_powershell(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(notify_module, "supported", lambda: True)
    monkeypatch.setattr(notify_module.subprocess, "run", fake_run)

    assert notify_windows("Titre", "Message accentué")

    arguments = calls[0]
    encoded = arguments[arguments.index("-EncodedCommand") + 1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "Titre" in script and "Message accentué" in script
    assert "-NoProfile" in arguments and "Hidden" in arguments


def test_notification_failure_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "supported", lambda: True)
    monkeypatch.setattr(
        notify_module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr=b"refus"),
    )

    assert notify_windows("Titre", "Message") is False


def test_notification_is_skipped_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(notify_module, "supported", lambda: False)

    assert notify_windows("Titre", "Message") is False


def test_scheduled_session_sends_a_notification(monkeypatch) -> None:
    with tmpdir("notification-planifiee") as tmp:
        from support import make_settings

        from core.autonomy import goals_path

        settings = make_settings(tmp)
        settings.autonomy.max_cycles = 1
        goals = goals_path(settings)
        goals.parent.mkdir(parents=True, exist_ok=True)
        goals.write_text("- ranger\n", encoding="utf-8")

        class _Reply:
            ok = True
            text = "fait"
            stop_reason = "final"
            error = ""

        class _Agent:
            bus = None
            registry = None
            run_id = "run-1"

            def run(self, task):
                return _Reply()

            def cancel(self) -> None:
                pass

            def reset_cancel(self) -> None:
                pass

        sent: list[tuple[str, str]] = []
        results = run_scheduled(
            settings,
            _Agent(),
            max_checks=1,
            now=lambda: __import__("datetime").datetime(2026, 1, 2, 2, 0),
            idle=lambda: 600.0,
            on_battery=lambda: False,
            sleep=lambda _s: None,
            notify=lambda title, message: sent.append((title, message)) or True,
        )

        assert results and len(sent) == 1
        assert "session" in sent[0][0].lower()
        assert "1 cycle" in sent[0][1]


def test_hotkey_callback_runs_and_errors_are_isolated() -> None:
    listener = HotkeyListener()
    calls: list[int] = []
    listener._callback = lambda: calls.append(1)
    listener._handle_hotkey()
    assert calls == [1]

    def broken() -> None:
        raise RuntimeError("callback casse")

    listener._callback = broken
    listener._handle_hotkey()  # ne doit pas lever


def test_hotkey_start_fails_cleanly_when_registration_is_refused() -> None:
    if os.name != "nt":
        listener = HotkeyListener()
        assert listener.start(lambda: None) is False
        assert "Windows" in listener.error
        return

    listener = HotkeyListener()
    listener._register_native = lambda: False  # type: ignore[method-assign]
    assert listener.start(lambda: None) is False
    assert "déjà utilisé" in listener.error
    listener.stop()

    # Un démarrage normal puis un arrêt ne doivent pas laisser de thread.
    normal = HotkeyListener()
    called = threading.Event()
    started = normal.start(called.set)
    if started:
        # Simule l'arrivée WM_HOTKEY dans la vraie boucle de messages : prouve
        # que le callback est bien branché, sans appui clavier humain.
        ctypes.windll.user32.PostThreadMessageW(
            normal._thread_id, 0x0312, 0xB17, 0
        )
        assert called.wait(2.0), "le message WM_HOTKEY n'a pas atteint le callback"
    normal.stop()
    assert normal.running is False


def test_global_signal_writes_stop_and_queues_the_ui_notice() -> None:
    from ui.app_gui import OmniAgentWindow

    with tmpdir("raccourci-signal") as tmp:
        from support import make_settings

        settings = make_settings(tmp)
        cancelled: list[bool] = []
        window = object.__new__(OmniAgentWindow)
        window.settings = settings
        window._autonomy_session = None
        window._hotkey_messages = queue.Queue()
        window.controller = SimpleNamespace(cancel=lambda: cancelled.append(True))

        window._global_stop_signal()

        assert stop_path(settings).is_file()
        assert cancelled == [True]
        assert window._hotkey_messages.get_nowait() == "stop"


def test_schedule_slot_guard_remains_independent_of_notification() -> None:
    schedule = Schedule(start_hour=1, end_hour=6)
    assert schedule.slot(__import__("datetime").datetime(2026, 1, 2, 2, 0)) == "2026-01-02"
