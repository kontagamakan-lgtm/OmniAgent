"""Mode autonome : objectifs, cycles, arrêt, garde-fous et journal."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from support import make_settings, tmpdir  # noqa: I001

from core.autonomy import (
    AutonomyJournal,
    AutonomySession,
    goals_path,
    journal_path,
    read_goals,
)


class _Reply:
    def __init__(self, ok: bool = True, text: str = "bilan", reason: str = "final") -> None:
        self.ok = ok
        self.text = text
        self.stop_reason = reason
        self.error = "" if ok else "echec simule"


class _Registry:
    """Faux registre : mémorise le confirmateur comme le vrai."""

    def __init__(self) -> None:
        self.confirm: object = "interface"

    def set_confirm(self, confirm: object) -> object:
        previous = self.confirm
        self.confirm = confirm
        return previous


class _Agent:
    def __init__(self, replies: list[_Reply], registry: _Registry | None = None) -> None:
        self._replies = list(replies)
        self.registry = registry
        self.tasks: list[str] = []
        self.run_id = ""
        self.denied = False

    def run(self, task: str) -> _Reply:
        self.tasks.append(task)
        self.run_id = f"run{len(self.tasks)}"
        if self.registry is not None:
            handler = self.registry.confirm
            assert callable(handler)
            self.denied = not handler(SimpleNamespace(name="shell_run"), {})
        return self._replies.pop(0)

    def cancel(self) -> None:
        return

    def reset_cancel(self) -> None:
        return


def _write_goals(settings, text: str = "- tenir un journal de bord des essais") -> Path:
    path = goals_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _journal_records(settings) -> list[dict]:
    text = journal_path(settings).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_a_session_runs_the_configured_cycles_and_journals_them() -> None:
    with tmpdir("autonomie") as tmp:
        settings = make_settings(tmp)
        _write_goals(settings)
        agent = _Agent([_Reply(text="premier bilan"), _Reply(text="second bilan")])
        session = AutonomySession(settings, agent)

        records = session.run(cycles=2, interval_s=0)

        assert len(agent.tasks) == 2
        assert "tenir un journal de bord" in agent.tasks[0]
        assert "premier bilan" in agent.tasks[1], "le cycle suivant doit voir l'historique"
        assert [record["type"] for record in records] == ["cycle_finished"] * 2
        assert records[-1]["ok"] is True and records[-1]["run_id"] == "run2"

        journal = _journal_records(settings)
        assert len(journal) == 4
        assert journal[-1]["cycle"] == 2 and journal[-1]["excerpt"] == "second bilan"


def test_no_goals_means_no_action() -> None:
    with tmpdir("autonomie-sans-objectif") as tmp:
        settings = make_settings(tmp)
        agent = _Agent([_Reply()])
        session = AutonomySession(settings, agent)

        records = session.run(cycles=3, interval_s=0)

        assert [record["type"] for record in records] == ["no_goals"]
        assert agent.tasks == [], "sans objectifs, le modèle n'est jamais appelé"
        journal = _journal_records(settings)
        assert journal[-1]["type"] == "no_goals"


def test_a_stop_request_interrupts_the_session() -> None:
    with tmpdir("autonomie-arret") as tmp:
        settings = make_settings(tmp)
        _write_goals(settings)
        agent = _Agent([_Reply(), _Reply(), _Reply()])
        session = AutonomySession(settings, agent)

        def stop_after_first(record: dict) -> None:
            if record.get("cycle") == 1:
                session.request_stop()

        records = session.run(cycles=3, interval_s=0, on_cycle=stop_after_first)

        assert len(agent.tasks) == 1, "l'arrêt doit être effectif entre deux cycles"
        assert len(records) == 1


def test_two_consecutive_failures_stop_the_session() -> None:
    with tmpdir("autonomie-echecs") as tmp:
        settings = make_settings(tmp)
        _write_goals(settings)
        agent = _Agent([_Reply(ok=False) for _ in range(3)])
        session = AutonomySession(settings, agent)

        records = session.run(cycles=3, interval_s=0)

        assert len(agent.tasks) == 2, "inutile de brûler un troisième cycle"
        assert [record.get("ok") for record in records if record["type"] == "cycle_finished"] == [
            False,
            False,
        ]


def test_dangerous_tools_are_denied_then_the_confirm_handler_is_restored() -> None:
    with tmpdir("autonomie-garde-fou") as tmp:
        settings = make_settings(tmp)
        _write_goals(settings)
        registry = _Registry()
        agent = _Agent([_Reply()], registry=registry)
        session = AutonomySession(settings, agent)

        session.run(cycles=1, interval_s=0)

        assert agent.denied is True, "un outil dangereux doit être refusé en autonomie"
        assert registry.confirm == "interface", "le confirmateur d'origine doit revenir"


def test_goals_can_come_from_a_configured_file() -> None:
    with tmpdir("autonomie-fichier") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.goals_file = "mes-objectifs.md"
        path = settings.paths.data_dir / "mes-objectifs.md"
        path.write_text("vérifier la documentation\n", encoding="utf-8")

        assert goals_path(settings) == path
        assert read_goals(path) == "vérifier la documentation"
        agent = _Agent([_Reply()])
        AutonomySession(settings, agent).run(cycles=1, interval_s=0)
        assert "vérifier la documentation" in agent.tasks[0]


def test_the_journal_tolerates_a_missing_directory() -> None:
    with tmpdir("autonomie-journal") as tmp:
        journal = AutonomyJournal(Path(tmp) / "absent" / "journal.jsonl")

        assert journal.append({"type": "cycle_finished", "ok": True}) is True
        records = journal.tail()

        assert len(records) == 1 and records[0]["ok"] is True


def test_autonomy_limits_are_validated() -> None:
    settings = make_settings(Path("."))
    settings.autonomy.max_cycles = 0
    settings.autonomy.interval_minutes = -5

    errors = {issue.field for issue in settings.validate() if issue.level == "error"}

    assert {"autonomy.max_cycles", "autonomy.interval_minutes"} <= errors
