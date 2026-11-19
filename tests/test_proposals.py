"""Propositions autonomes : aucune exécution sans approbation humaine."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from support import make_settings, tmpdir  # noqa: I001

from core.autonomy import goals_path
from core.proposals import (
    ProposalStore,
    approve_proposal,
    parse_proposal,
    pending_proposals,
    propose,
    proposals_path,
    reject_proposal,
)


class _LLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.calls.append(list(messages))
        return SimpleNamespace(text=self.text)


class _Agent:
    """Agent factice : le mode proposition ne doit jamais appeler `run`."""

    def __init__(self, text: str) -> None:
        self.llm = _LLM(text)
        self.runs: list[str] = []

    def run(self, task: str):
        self.runs.append(task)
        raise AssertionError("aucune tâche ne doit être exécutée en mode proposition")


def _goals(settings, text: str = "- écrire un état des lieux hebdomadaire") -> None:
    path = goals_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def test_a_proposal_is_recorded_pending_and_nothing_runs() -> None:
    with tmpdir("proposition") as tmp:
        settings = make_settings(tmp)
        _goals(settings)
        agent = _Agent(
            json.dumps(
                {
                    "task": "Inventorier les documents non classés",
                    "tools": ["list_directory", "read_file"],
                    "risk": "faible",
                    "why": "prépare l'état des lieux",
                }
            )
        )

        record, error = propose(settings, agent)

        assert error == "" and record is not None
        assert record["state"] == "pending" and len(record["id"]) == 8
        assert record["tools"] == ["list_directory", "read_file"]
        assert agent.runs == [], "proposer ne doit exécuter aucun outil"
        assert len(agent.llm.calls) == 1
        assert "état des lieux" in agent.llm.calls[0][-1]["content"]

        stored = pending_proposals(settings)
        assert [item["id"] for item in stored] == [record["id"]]


def test_without_goals_the_model_is_not_called() -> None:
    with tmpdir("proposition-sans-objectif") as tmp:
        settings = make_settings(tmp)
        agent = _Agent("{}")

        record, error = propose(settings, agent)

        assert record is None and "objectif" in error
        assert agent.llm.calls == []


def test_a_malformed_answer_still_yields_a_readable_proposal() -> None:
    proposal = parse_proposal("Je propose de ranger le dossier Documents.")

    assert proposal["task"].startswith("Je propose")
    assert proposal["risk"] == "inconnu"
    assert proposal["tools"] == []


def test_approval_and_rejection_change_only_the_state() -> None:
    with tmpdir("proposition-etats") as tmp:
        settings = make_settings(tmp)
        _goals(settings)
        agent = _Agent(json.dumps({"task": "Tâche A", "tools": [], "risk": "moyen"}))
        first, _ = propose(settings, agent)
        other = _Agent(json.dumps({"task": "Tâche B", "tools": [], "risk": "moyen"}))
        second, _ = propose(settings, other)

        approved = approve_proposal(settings, first["id"])
        assert approved is not None and approved["state"] == "approved"
        assert [item["id"] for item in pending_proposals(settings)] == [second["id"]]

        rejected = reject_proposal(settings, second["id"])
        assert rejected is not None and rejected["state"] == "rejected"
        assert pending_proposals(settings) == []
        assert approve_proposal(settings, "inconnu") is None


def test_the_proposal_store_is_bounded_and_survives_bad_lines() -> None:
    with tmpdir("proposition-borne") as tmp:
        path = Path(tmp) / "proposals.jsonl"
        store = ProposalStore(path, max_entries=2)
        for number in range(3):
            store.append({"id": str(number), "type": "proposal", "state": "pending"})
        with path.open("a", encoding="utf-8") as handle:
            handle.write("pas du json\n")

        records = store.all()

        assert [record["id"] for record in records] == ["1", "2"]
        assert proposals_path(make_settings(tmp)).name == "proposals.jsonl"


def test_approval_from_the_command_line_runs_the_stored_task(monkeypatch) -> None:
    import app as app_module
    import config as config_module

    with tmpdir("approbation-cli") as tmp:
        settings = make_settings(tmp)
        store = ProposalStore(proposals_path(settings))
        store.append(
            {
                "type": "proposal",
                "id": "abc12345",
                "state": "pending",
                "task": "Ranger les rapports",
                "tools": [],
                "risk": "faible",
                "why": "",
            }
        )
        order: list[str] = []
        monkeypatch.setattr(
            config_module.Settings,
            "load",
            classmethod(lambda cls, *args, **kwargs: settings),
        )
        monkeypatch.setattr(
            app_module.config, "configure_logging", lambda *args, **kwargs: order.append("logging")
        )
        monkeypatch.setattr(
            app_module,
            "_run_task",
            lambda s, a: (order.append(f"task:{a.task}"), 0)[1],
        )

        code = app_module.main(["--autonomy-approve", "abc12345"])

        assert code == 0
        assert order == ["logging", "task:Ranger les rapports"], (
            "le journal doit être configuré avant l'exécution de la tâche approuvée"
        )
        assert store.find("abc12345")["state"] == "executed"


def test_approving_an_unknown_proposal_changes_nothing(monkeypatch) -> None:
    import app as app_module
    import config as config_module

    with tmpdir("approbation-inconnue") as tmp:
        settings = make_settings(tmp)
        calls: list[str] = []
        monkeypatch.setattr(
            config_module.Settings,
            "load",
            classmethod(lambda cls, *args, **kwargs: settings),
        )
        monkeypatch.setattr(app_module.config, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr(
            app_module, "_run_task", lambda s, a: (calls.append(a.task), 0)[1]
        )

        code = app_module.main(["--autonomy-approve", "inconnu"])

        assert code == 2
        assert calls == [], "une proposition introuvable ne lance aucune tâche"
