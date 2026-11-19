"""Identité Windows, actions de toast et repli sans PowerShell."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import core.notify as notify  # noqa: E402
from core.stopfile import stop_path  # noqa: E402
from core.notify import (  # noqa: E402
    APP_USER_MODEL_ID,
    ToastAction,
    build_protocol_script,
    build_shortcut_script,
    build_toast_script,
    install_notifications,
    parse_toast_action,
)


def test_toast_script_carries_actions_and_escapes_content() -> None:
    script = build_toast_script(
        "Titre <x>",
        "Fin & suite",
        actions=(
            ToastAction('Ouvrir "trace"', "omniagent://trace/run-1"),
            ToastAction("Arrêter", "omniagent://stop"),
        ),
    )
    assert 'activationType="protocol"' in script
    assert 'arguments="omniagent://trace/run-1"' in script
    assert "omniagent://stop" in script
    assert "&lt;x&gt;" in script and "&amp;" in script
    assert 'content="Ouvrir &quot;trace&quot;"' in script
    assert APP_USER_MODEL_ID in script


def test_parse_action_refuses_anything_but_our_protocol() -> None:
    assert parse_toast_action("omniagent://trace/run-1") == ("trace", "run-1")
    assert parse_toast_action("omniagent://stop") == ("stop", "")
    assert parse_toast_action("omniagent://stop/") == ("stop", "")
    assert parse_toast_action("omniagent://trace/run%201") == ("trace", "run 1")
    assert parse_toast_action("https://example.com/trace/1") is None
    assert parse_toast_action("omniagent://delete/run-1") is None
    assert parse_toast_action("") is None


def test_parse_action_rejects_path_traversal_and_extra_parts() -> None:
    for uri in (
        "omniagent://trace/",
        "omniagent://trace",
        "omniagent://trace/../run",
        "omniagent://trace/..%2Frun",
        "omniagent://trace/run/extra",
        "omniagent://trace/run?x=1",
        "omniagent://trace/run#fragment",
        "omniagent://trace/run%00",
        "omniagent://stop/extra",
        "omniagent://stop?x=1",
        "omniagent://user@trace/run",
    ):
        assert parse_toast_action(uri) is None, uri


def test_shortcut_script_sets_the_app_user_model_id() -> None:
    destination = Path("C:/Users/test/Start Menu/Programs/OmniAgent.lnk")
    script = build_shortcut_script(
        r"C:\Python\pythonw.exe",
        r'"C:\app\app.py"',
        destination=destination,
    )
    assert "WScript.Shell" in script
    assert str(destination).replace("\\", "/") in script.replace("\\", "/")
    assert "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3" in script
    assert APP_USER_MODEL_ID in script
    assert "IPropertyStore" in script


def test_protocol_script_uses_hkcu_and_quotes_the_command() -> None:
    script = build_protocol_script(r"C:\Python\python.exe")
    assert "HKCU:\\Software\\Classes\\omniagent" in script
    assert "URL Protocol" in script
    assert "--notify-action" in script and "%1" in script


def test_install_is_idempotent_in_its_commands_and_reports_failure(monkeypatch) -> None:
    seen: list[str] = []

    def fake(script: str, *, timeout_s: float = 20.0):
        seen.append(script)
        return True, "ok"

    monkeypatch.setattr(notify, "supported", lambda: True)
    monkeypatch.setattr(notify, "_run_script", fake)
    ok, message = install_notifications(r"C:\Python\pythonw.exe", r'"C:\app\app.py"')
    assert ok and "omniagent" in message
    assert len(seen) == 2
    assert "WScript.Shell" in seen[0]
    assert "HKCU:\\Software\\Classes" in seen[1]
    assert "C:\\app\\app.py" in seen[1]
    assert "--notify-action" in seen[1]

    monkeypatch.setattr(notify, "_run_script", lambda *a, **k: (False, "refus"))
    ok, message = install_notifications("x")
    assert not ok and "refus" in message


def test_enriched_toast_falls_back_to_a_plain_one(monkeypatch) -> None:
    outcomes = iter([(False, "actions refusees"), (True, "ok")])
    scripts: list[str] = []

    def fake(script: str, *, timeout_s: float = 20.0):
        scripts.append(script)
        return next(outcomes)

    monkeypatch.setattr(notify, "supported", lambda: True)
    monkeypatch.setattr(notify, "_run_script", fake)
    assert notify.notify_windows("T", "M", actions=(ToastAction("Arrêter", "omniagent://stop"),))
    assert len(scripts) == 2
    assert "activationType" not in scripts[1]


def test_notification_is_never_raised_without_powershell(monkeypatch) -> None:
    monkeypatch.setattr(notify, "supported", lambda: True)

    def broken(*args, **kwargs):
        raise OSError("powershell absent")

    monkeypatch.setattr(notify.subprocess, "run", broken)
    assert notify.notify_windows("T", "M") is False
    ok, message = install_notifications("x")
    assert not ok and "powershell" in message.lower() or "raccourci" in message


def test_scheduled_notification_offers_the_run_trace(monkeypatch) -> None:
    captured: list[tuple[ToastAction, ...]] = []

    def fake(title, message, *, timeout_s=15.0, actions=()):
        captured.append(tuple(actions))
        return True

    monkeypatch.setattr(notify, "notify_windows", fake)
    notify.notify_scheduled_session(
        cycles=1, cost=0.01, ok=False, duration_s=2.0, run_id="run-9"
    )
    uris = [action.uri for action in captured[0]]
    assert "omniagent://trace/run-9" in uris
    assert "omniagent://stop" in uris


def test_app_handles_a_trace_action(monkeypatch) -> None:
    import app

    with tmpdir("notify-action") as tmp:
        settings = make_settings(tmp)
        runs = settings.paths.log_dir / "runs"
        runs.mkdir(parents=True)
        trace = runs / "run-7.jsonl"
        trace.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(
            notify.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
        )
        assert app._notify_action(settings, "omniagent://trace/run-7") == 0
        assert app._notify_action(settings, "omniagent://trace/inconnu") == 2
        assert app._notify_action(settings, "https://example.com") == 2
        assert app._notify_action(settings, "omniagent://trace/../run-7") == 2


def test_app_stop_action_writes_stop_without_a_trace() -> None:
    import app

    with tmpdir("notify-stop") as tmp:
        settings = make_settings(tmp)
        assert app._notify_action(settings, "omniagent://stop") == 0
        assert stop_path(settings).is_file()


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
