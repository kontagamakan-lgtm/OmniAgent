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
from core.approval import ApprovalGate
from core.events import EventBus, EventType
from core.tool_registry import ToolRegistry
from tools.base import FunctionTool


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


class _ApprovalAgent:
    """Agent réel minimal : dispatche un outil dangereux pendant chaque cycle."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.tasks: list[str] = []
        self.run_id = ""
        self.result = None

    def run(self, task: str) -> _Reply:
        self.tasks.append(task)
        self.run_id = f"run{len(self.tasks)}"
        self.result = self.registry.dispatch("fs_delete", {"path": "test.txt"})
        return _Reply()

    def cancel(self) -> None:
        return

    def reset_cancel(self) -> None:
        return


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


class _BudgetAgent(_Agent):
    """Faux agent qui publie une consommation comme le vrai, sur son bus."""

    def __init__(
        self,
        reply: _Reply,
        *,
        prompt_tokens: int = 0,
        tool_started: int = 0,
    ) -> None:
        super().__init__([reply])
        self.bus = EventBus()
        self.prompt_tokens = prompt_tokens
        self.tool_started = tool_started
        self.cancelled = False

    def run(self, task: str) -> _Reply:
        self.bus.emit(
            EventType.LLM_RESPONSE,
            {"tokens": {"prompt": self.prompt_tokens, "completion": 0}},
            run_id="budget",
        )
        for _ in range(self.tool_started):
            self.bus.emit(EventType.TOOL_STARTED, {}, run_id="budget")
        return super().run(task)

    def cancel(self) -> None:
        self.cancelled = True


def test_a_token_budget_stops_the_session_between_cycles() -> None:
    with tmpdir("autonomie-budget") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.max_total_tokens = 100
        settings.autonomy.max_tokens_per_cycle = 0
        _write_goals(settings)
        agent = _BudgetAgent(_Reply(), prompt_tokens=60)

        records = AutonomySession(settings, agent).run(cycles=3, interval_s=0)

        assert len(agent.tasks) == 2, "le troisième cycle dépasserait le plafond"
        assert [record["type"] for record in records] == [
            "cycle_finished",
            "cycle_finished",
            "budget_exhausted",
        ]
        assert records[-1]["tokens"] == 120 and records[-1]["limit"] == 100
        assert _journal_records(settings)[-1]["type"] == "budget_exhausted"


def test_a_cycle_token_budget_cancels_the_run() -> None:
    with tmpdir("autonomie-budget-cycle") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.max_total_tokens = 0
        settings.autonomy.max_tokens_per_cycle = 50
        _write_goals(settings)
        agent = _BudgetAgent(_Reply(), prompt_tokens=60)

        records = AutonomySession(settings, agent).run(cycles=1, interval_s=0)

        assert agent.cancelled is True
        assert records[0]["budget"] == "tokens"
        assert records[0]["tokens"] == 60


def test_a_tool_budget_cancels_the_run() -> None:
    with tmpdir("autonomie-budget-outils") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.max_total_tokens = 0
        settings.autonomy.max_tool_calls_per_cycle = 2
        _write_goals(settings)
        agent = _BudgetAgent(_Reply(), tool_started=3)

        records = AutonomySession(settings, agent).run(cycles=1, interval_s=0)

        assert agent.cancelled is True
        assert records[0]["budget"] == "tools"
        assert records[0]["tool_calls"] == 3


def test_autonomy_restores_the_interface_approval_decider() -> None:
    with tmpdir("autonomie-approbation-interface") as tmp:
        settings = make_settings(tmp)
        _write_goals(settings)
        calls: list[str] = []
        seen: list[str] = []

        def ui_decider(request) -> str:
            seen.append(request.tool)
            return "once"

        def delete(path: str = "") -> str:
            calls.append(path)
            return "supprime"

        tool = FunctionTool(
            "fs_delete",
            "Supprime un fichier.",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            delete,
            dangerous=True,
            read_only=False,
        )
        gate = ApprovalGate(decide=ui_decider, mode="dangerous", settings=settings)
        # Même branchement que l'interface : la porte est installée après coup.
        registry = ToolRegistry([tool])
        registry.set_approval(gate)
        agent = _ApprovalAgent(registry)

        AutonomySession(settings, agent).run(cycles=1, interval_s=0)

        assert agent.result is not None and agent.result.ok is False
        assert agent.result.error_code == "confirmation_required"
        assert calls == [], "l'outil dangereux reste refusé pendant la session"
        assert gate.decide is ui_decider, (
            "la restauration ne doit pas remettre gate.decide à None"
        )

        after = registry.dispatch("fs_delete", {"path": "test.txt"})
        assert after.ok is True and calls == ["test.txt"]
        assert seen == ["fs_delete"], "l'interface doit redemander après la session"


def test_the_confirm_handler_is_restored_to_none() -> None:
    with tmpdir("autonomie-sans-confirmateur") as tmp:
        settings = make_settings(tmp)
        _write_goals(settings)
        registry = _Registry()
        registry.confirm = None  # aucune confirmation armée avant la session
        agent = _Agent([_Reply()], registry=registry)
        session = AutonomySession(settings, agent)

        session.run(cycles=1, interval_s=0)

        assert agent.denied is True, "le refus s'applique pendant la session"
        assert registry.confirm is None, (
            "sans confirmateur avant, il ne doit pas en rester un après"
        )


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


def test_the_journal_keeps_only_the_latest_entries() -> None:
    with tmpdir("journal-borne") as tmp:
        path = Path(tmp) / "journal.jsonl"
        journal = AutonomyJournal(path, max_entries=3)

        for number in range(1, 7):
            journal.append({"type": "cycle_finished", "cycle": number, "ok": True})

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3, f"journal non borné : {len(lines)} entrées"
        assert [json.loads(line)["cycle"] for line in lines] == [4, 5, 6]


def test_the_journal_redacts_keys_in_free_text() -> None:
    """L'extrait d'un cycle recopie la réponse du modèle : elle peut contenir une clé."""
    with tmpdir("journal-secrets") as tmp:
        path = Path(tmp) / "journal.jsonl"
        key = "AQ.AbCdEf0123456789AbCdEf0123456789"
        AutonomyJournal(path).append(
            {
                "type": "cycle_finished",
                "cycle": 1,
                "ok": False,
                "excerpt": f"réponse : la clé {key} a été refusée",
            }
        )

        body = path.read_text(encoding="utf-8")
        assert key not in body, body
        assert json.loads(body.splitlines()[0])["cycle"] == 1


def test_the_journal_is_bounded_by_size_but_keeps_the_last_cycle() -> None:
    with tmpdir("journal-taille") as tmp:
        path = Path(tmp) / "journal.jsonl"
        journal = AutonomyJournal(path, max_entries=100, max_bytes=600)

        for number in range(10):
            journal.append(
                {
                    "type": "cycle_finished",
                    "cycle": number,
                    "ok": True,
                    "excerpt": "x" * 120,
                }
            )

        lines = path.read_text(encoding="utf-8").splitlines()
        assert path.stat().st_size <= 600, f"journal de {path.stat().st_size} octets"
        assert lines, "le dernier cycle doit toujours rester"
        assert json.loads(lines[-1])["cycle"] == 9


def test_a_session_journal_respects_the_configured_bound() -> None:
    with tmpdir("journal-session") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.journal_max_entries = 4
        _write_goals(settings)
        agent = _Agent([_Reply() for _ in range(4)])

        AutonomySession(settings, agent).run(cycles=4, interval_s=0)

        records = _journal_records(settings)
        assert len(records) == 4, f"journal non borné : {len(records)} entrées"
        assert records[-1]["type"] == "cycle_finished"
        assert records[-1]["cycle"] == 4


def test_the_autonomy_journal_can_be_read_in_the_interface() -> None:
    from ui.app_gui import format_autonomy_journal

    records = [
        {"type": "cycle_started", "cycle": 1, "at": "2026-11-19T10:00:00+00:00"},
        {
            "type": "cycle_finished",
            "cycle": 1,
            "ok": True,
            "stop_reason": "final",
            "run_id": "abc",
            "excerpt": "bilan un",
            "at": "2026-11-19T10:05:00+00:00",
        },
        {
            "type": "cycle_finished",
            "cycle": 2,
            "ok": False,
            "stop_reason": "llm_error",
            "run_id": "def",
            "excerpt": "",
            "error": "panne simulee",
            "at": "2026-11-19T11:00:00+00:00",
        },
    ]

    text = format_autonomy_journal(records)

    assert text.index("Cycle 2") < text.index("Cycle 1"), "le plus récent d'abord"
    assert "échec (llm_error)" in text and "run def" in text
    assert "erreur : panne simulee" in text
    assert "bilan un" in text
    assert format_autonomy_journal([]) == "Aucun cycle autonome enregistré."


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
    settings.autonomy.journal_max_entries = 0
    settings.autonomy.journal_max_bytes = 10

    errors = {issue.field for issue in settings.validate() if issue.level == "error"}

    assert {
        "autonomy.max_cycles",
        "autonomy.interval_minutes",
        "autonomy.journal_max_entries",
        "autonomy.journal_max_bytes",
    } <= errors
