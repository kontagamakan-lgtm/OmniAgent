"""Tâche planifiée Windows : définition, appels simulés, désinscription UI."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

import core.taskscheduler as tasks  # noqa: E402


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_definition_triggers_logon_wake_and_repeats_in_the_window() -> None:
    xml = tasks.build_task_xml(
        python=r"C:\Python\python.exe",
        arguments=r'"C:\app\app.py" --autonomy-schedule --schedule-checks 1',
        workdir=r"C:\app",
        user=r"PC\jean",
        start_hour=1,
        end_hour=6,
        now=datetime(2026, 11, 19, 22, 0),
    )

    assert "<LogonTrigger>" in xml
    assert "<UserId>PC\\jean</UserId>" in xml
    # Prochaine ouverture de fenêtre : le lendemain à 1 h.
    assert "<StartBoundary>2026-11-20T01:00:00</StartBoundary>" in xml
    assert "<Interval>PT1H</Interval>" in xml
    assert "<Duration>PT5H</Duration>" in xml
    # Réveil et rattrapage : Windows exécute la tâche au réveil et après une
    # fenêtre manquée ; `core.schedule` décide ensuite du rattrapage.
    assert "<WakeToRun>true</WakeToRun>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    # Verrou anti-double exécution côté Windows, en plus de `sessions.jsonl`.
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "--autonomy-schedule --schedule-checks 1" in xml
    assert tasks.TASK_NAME in xml


def test_definition_escapes_paths_and_arguments() -> None:
    xml = tasks.build_task_xml(
        python=r"C:\a&b\python.exe",
        arguments='--x "<y>"',
        workdir=r"C:\dossier & <test>",
        now=datetime(2026, 1, 1, 0, 0),
    )

    assert "a&amp;b" in xml
    assert "&lt;y&gt;" in xml
    assert "&amp;" in xml and "&lt;test&gt;" in xml


def test_install_writes_the_definition_then_calls_schtasks(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "supported", lambda: True)
    with tmpdir("tache-install") as tmp:
        settings = make_settings(tmp)
        calls: list[list[str]] = []

        def runner(args: list[str]) -> SimpleNamespace:
            calls.append(list(args))
            return _result()

        ok, message = tasks.install_task(
            settings, user=r"PC\jean", runner=runner
        )
        definition = settings.paths.cache_dir / "omniagent-task.xml"

        assert ok is True and "enregistree" in message
        assert definition.is_file()
        content = definition.read_text(encoding="utf-16")
        assert "<LogonTrigger>" in content
        assert calls == [
            [
                "schtasks",
                "/Create",
                "/TN",
                tasks.TASK_NAME,
                "/XML",
                str(definition),
                "/F",
            ]
        ]


def test_install_reports_a_refusal_without_raising(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "supported", lambda: True)
    with tmpdir("tache-refus") as tmp:
        settings = make_settings(tmp)
        ok, message = tasks.install_task(
            settings,
            runner=lambda args: _result(1, stderr="ERREUR: acces refuse."),
        )

    assert ok is False
    assert "acces refuse" in message


def test_uninstall_deletes_the_registered_task(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "supported", lambda: True)
    calls: list[list[str]] = []

    def runner(args: list[str]) -> SimpleNamespace:
        calls.append(list(args))
        return _result()

    ok, message = tasks.uninstall_task(runner=runner)

    assert ok is True and "desinscrite" in message
    assert calls == [["schtasks", "/Delete", "/TN", tasks.TASK_NAME, "/F"]]


def test_nothing_runs_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "supported", lambda: False)

    def runner(args: list[str]) -> SimpleNamespace:  # pragma: no cover
        raise AssertionError("aucun processus ne doit être lancé hors Windows")

    with tmpdir("tache-hors-windows") as tmp:
        settings = make_settings(tmp)
        ok, message = tasks.install_task(settings, runner=runner)
        assert ok is False and "Windows" in message
        assert not (settings.paths.cache_dir / "omniagent-task.xml").exists()

    assert tasks.uninstall_task(runner=runner)[0] is False


def test_the_interface_registers_and_unregisters(monkeypatch) -> None:
    from ui.app_gui import OmniAgentWindow

    with tmpdir("tache-interface") as tmp:
        settings = make_settings(tmp)
        messages: list[tuple[str, str]] = []
        statuses: list[str] = []
        window = object.__new__(OmniAgentWindow)
        window.settings = settings
        window._append = lambda kind, text: messages.append((kind, text))
        window._set_status = lambda text: statuses.append(text)

        monkeypatch.setattr(
            tasks, "install_task", lambda settings: (True, "tache enregistree : X")
        )
        window._install_schedule_task()
        monkeypatch.setattr(tasks, "uninstall_task", lambda: (False, "refus"))
        window._uninstall_schedule_task()

    assert messages == [("système", "tache enregistree : X"), ("erreur", "refus")]
    assert statuses == ["tâche planifiée", "désinscription de la tâche refusée"]
