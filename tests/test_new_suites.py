"""Suites A, B, C, D, F, G, H, I, J, K, L, M, N, Q de la revue."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from support import make_settings, tmpdir  # noqa: I001

from core.anomalies import detect_anomalies
from core.autonomy import AutonomyJournal, AutonomySession, goals_path, journal_path
from core.backup import (
    BACKUP_MAGIC,
    create_backup,
    inspect_backup,
    restore_backup,
)
from core.cost import estimate_cost
from core.export import import_bundle, validate_bundle
from core.index_doctor import IndexIssue, explain, format_issues
from core.index_repair import repair
from core.policy import ToolPolicy, load_policy, policy_path
from core.proposals import (
    ProposalStore,
    approve_proposal,
    finish_proposal,
    pending_proposals,
    proposals_path,
    propose,
    set_proposal_task,
)
from core.resume import build_plan, resume_prompt
from core.run_index import INDEX_NAME, RunIndex, merged_entries
from core.schedule import Schedule, should_start
from core.stopfile import clear_stop, request_stop, stop_requested
from core.trace import RunTrace, read_trace
from core.trace_migrate import migrate_file


class _LLM:
    def __init__(self, text: str = "{}") -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.calls.append(list(messages))
        return SimpleNamespace(text=self.text)


class _Agent:
    def __init__(self, text: str = "{}") -> None:
        self.llm = _LLM(text)
        self.runs: list[str] = []

    def run(self, task: str):
        self.runs.append(task)
        raise AssertionError("la proposition ne doit pas executer d'outil")


# -- A : index multi-espaces de travail -------------------------------------


def test_workspace_indexes_are_merged_for_traces_panel() -> None:
    with tmpdir("index-ateliers") as tmp:
        root = Path(tmp) / "runs"
        workspace = root / "projet-abc"
        workspace.mkdir(parents=True)
        RunIndex(root).mark("racine", "finished", path=root / "racine.jsonl")
        RunIndex(workspace).mark(
            "atelier", "finished", path=workspace / "atelier.jsonl", manifest={"model": "m2"}
        )

        entries = merged_entries(root)

        assert set(entries) == {"racine", "atelier"}
        assert entries["atelier"].model == "m2"


# -- B : politique d'outils --------------------------------------------------


def test_policy_allows_a_narrow_rule_and_denies_the_rest() -> None:
    policy = ToolPolicy(
        rules=[
            {"tool": "fs_write", "action": "allow", "args": {"path": ["reports/"]}},
            {"tool": "shell_run", "action": "allow", "args": {"command": ["dir"]}},
            {"tool": "*", "action": "deny"},
        ]
    )

    assert policy.decide("fs_write", {"path": "reports/etat.md"})[0]
    assert not policy.decide("fs_write", {"path": "C:/Windows/system.ini"})[0]
    assert policy.decide("shell_run", {"command": "dir C:/"})[0]
    assert not policy.decide("shell_run", {"command": "format C:"})[0]
    assert not policy.decide("vault_read", {})[0]

    limited = ToolPolicy(
        rules=[{"tool": "fs_write", "action": "allow", "max_calls": 1}]
    )
    assert limited.decide("fs_write", {"path": "reports/a"})[0]
    allowed, rule = limited.decide("fs_write", {"path": "reports/b"})
    assert not allowed and "plafond" in rule


def test_missing_or_broken_policy_denies_everything() -> None:
    with tmpdir("politique") as tmp:
        settings = make_settings(tmp)
        assert not load_policy(settings).decide("fs_write", {"path": "reports/x"})[0]
        path = policy_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ pas du json", encoding="utf-8")
        assert load_policy(settings).rules == []
        path.write_text(
            json.dumps({"rules": [{"tool": "fs_write", "action": "allow"}]}),
            encoding="utf-8",
        )
        assert load_policy(settings).decide("fs_write", {"path": "x"})[0]


def test_session_journals_each_policy_decision() -> None:
    with tmpdir("politique-session") as tmp:
        settings = make_settings(tmp)
        policy_path(settings).parent.mkdir(parents=True, exist_ok=True)
        policy_path(settings).write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "tool": "fs_write",
                            "action": "allow",
                            "args": {"path": ["reports/"]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        class _NoRun:
            bus = None
            registry = None

            def cancel(self) -> None:
                pass

            def reset_cancel(self) -> None:
                pass

        session = AutonomySession(settings, _NoRun())
        tool = SimpleNamespace(name="fs_write")

        assert session._authorize(tool, {"path": "reports/x.md"})
        assert not session._authorize(tool, {"path": "Windows/x.ini"})
        decisions = [
            record
            for record in AutonomyJournal(journal_path(settings)).tail()
            if record.get("type") == "policy_decision"
        ]
        assert [record["allowed"] for record in decisions] == [True, False]


# -- C : reparations ---------------------------------------------------------


def test_repair_previews_then_rebuilds_a_missing_index() -> None:
    with tmpdir("reparation") as tmp:
        root = Path(tmp)
        (root / "runs").mkdir()
        (root / "runs" / "a.jsonl").write_text(
            json.dumps(
                {
                    "run_id": "a",
                    "type": "run_started",
                    "seq": 1,
                    "payload": {"model": "m", "trace_format": 3},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        preview = repair(root / "runs", apply=False)
        assert preview and not (root / "runs" / INDEX_NAME).exists()

        applied = repair(root / "runs", apply=True)
        assert applied and (root / "runs" / INDEX_NAME).read_text(encoding="utf-8")
        assert "a" in RunIndex(root / "runs").entries()


def test_doctor_codes_come_with_a_fix() -> None:
    assert "--index-compact" in explain("index_orphan")
    assert "remede" in format_issues(
        [IndexIssue("warning", "active_stale", "x")],
        with_help=True,
    )


# -- D : sauvegarde / restauration ------------------------------------------


def test_backup_is_encrypted_and_restores_only_after_verification() -> None:
    with tmpdir("sauvegarde") as tmp, tmpdir("restauration") as other:
        settings = make_settings(tmp)
        (tmp / "config.json").write_text('{"app": "test"}\n', encoding="utf-8")
        (tmp / "logs" / "runs").mkdir(parents=True)
        (tmp / "logs" / "runs" / "a.jsonl").write_text(
            '{"run_id": "a", "type": "run_started", "seq": 1, "payload": {}}\n',
            encoding="utf-8",
        )
        archive = create_backup(settings, Path(tmp) / "save.ombak", passphrase="secret")
        assert archive.read_bytes().startswith(BACKUP_MAGIC)
        assert not inspect_backup(archive, passphrase="faux")["ok"]
        assert inspect_backup(archive, passphrase="secret")["ok"]

        target = make_settings(other)
        preview = restore_backup(target, archive, passphrase="secret", apply=False)
        assert preview["ok"] and not (other / "config.json").exists()

        applied = restore_backup(target, archive, passphrase="secret", apply=True)
        assert applied["ok"] and (other / "config.json").read_text(encoding="utf-8")


# -- F et L : cout et anomalies ---------------------------------------------


def test_cost_uses_the_model_table_and_anomalies_raise_alerts() -> None:
    assert estimate_cost("deepseek-chat", 1_000_000, 0) == 0.27
    assert estimate_cost("modele-inconnu", 1_000_000, 0) == 0.0
    records = [
        {"ok": False, "tool_errors": {"shell_run": 2}, "trims": 0, "duration_s": 1}
        for _ in range(5)
    ]
    alerts = detect_anomalies(records)
    assert any("taux d'echec" in alert for alert in alerts)
    assert any("shell_run" in alert for alert in alerts)


# -- G et H : planification et arret d'urgence ------------------------------


def test_schedule_window_idle_ac_and_one_session_per_slot() -> None:
    schedule = Schedule(start_hour=1, end_hour=6, min_idle_s=300, require_ac=True)
    now = datetime(2026, 1, 2, 2, 0)
    assert should_start(schedule, now, idle_s=600, on_battery=False)[0]
    assert not should_start(schedule, now, idle_s=10, on_battery=False)[0]
    assert not should_start(schedule, now, idle_s=600, on_battery=True)[0]
    assert not should_start(
        schedule, now, idle_s=600, on_battery=False, last_slot="2026-01-02"
    )[0]
    after_midnight = Schedule(start_hour=22, end_hour=6)
    assert after_midnight.slot(datetime(2026, 1, 3, 1)) == "2026-01-02"


def test_stop_file_prevents_a_new_autonomy_session() -> None:
    with tmpdir("arret") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.max_cycles = 1
        goals = goals_path(settings)
        goals.parent.mkdir(parents=True, exist_ok=True)
        goals.write_text("- ranger les rapports\n", encoding="utf-8")

        class _NoRun:
            bus = None
            registry = None

            def run(self, task):  # pragma: no cover - ne doit jamais partir
                raise AssertionError("la session ne doit pas demarrer")

            def cancel(self) -> None:
                pass

            def reset_cancel(self) -> None:
                pass

        request_stop(settings, "test")
        assert stop_requested(settings)
        session = AutonomySession(settings, _NoRun())
        records = session.run(cycles=1, interval_s=0)

        assert records == []
        assert any(
            record.get("type") == "stop_requested"
            for record in AutonomyJournal(journal_path(settings)).tail()
        )
        assert clear_stop(settings)


def test_scheduled_runner_starts_once_and_records_the_slot() -> None:
    from core.schedule import run_scheduled, sessions_path

    with tmpdir("planification") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.max_cycles = 1
        goals = goals_path(settings)
        goals.parent.mkdir(parents=True, exist_ok=True)
        goals.write_text("- ranger les rapports\n", encoding="utf-8")

        class _Reply:
            ok = True
            text = "rapport range"
            stop_reason = "final"
            error = ""

        class _Agent:
            bus = None
            registry = None
            run_id = "run-1"

            def __init__(self) -> None:
                self.tasks: list[str] = []

            def run(self, task: str):
                self.tasks.append(task)
                return _Reply()

            def cancel(self) -> None:
                pass

            def reset_cancel(self) -> None:
                pass

        agent = _Agent()
        results = run_scheduled(
            settings,
            agent,
            max_checks=1,
            now=lambda: datetime(2026, 1, 2, 2, 0),
            idle=lambda: 600.0,
            on_battery=lambda: False,
            sleep=lambda _seconds: None,
        )

        assert len(agent.tasks) == 1
        assert results and results[0]["slot"] == "2026-01-02"
        assert "2026-01-02" in sessions_path(settings).read_text(encoding="utf-8")


# -- I : cycle de vie des propositions --------------------------------------


def test_proposals_expire_deduplicate_and_record_their_run() -> None:
    with tmpdir("cycle-propositions") as tmp:
        settings = make_settings(tmp)
        goals = goals_path(settings)
        goals.parent.mkdir(parents=True, exist_ok=True)
        goals.write_text("- objectif\n", encoding="utf-8")
        agent = _Agent(json.dumps({"task": "Ranger les rapports", "tools": [], "risk": "faible"}))

        first, _ = propose(settings, agent)
        second, _ = propose(settings, agent)
        assert second["id"] == first["id"], "la meme tache ne doit pas etre dupliquee"

        edited = set_proposal_task(settings, first["id"], "Ranger les rapports de janvier")
        assert edited and edited["task"].endswith("janvier")
        assert approve_proposal(settings, first["id"])["state"] == "approved"
        done = finish_proposal(settings, first["id"], ok=True, run_id="run-1")
        assert done["state"] == "executed" and done["run_id"] == "run-1"

        old = ProposalStore(proposals_path(settings))
        old.append(
            {
                "type": "proposal",
                "id": "vieux",
                "state": "pending",
                "task": "Vieille tache",
                "at": (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat(),
            }
        )
        assert pending_proposals(settings) == []
        assert old.find("vieux")["state"] == "expired"


# -- J : rotation verifiee ---------------------------------------------------


def test_rotation_verification_and_key_backup(tmp_path: Path) -> None:
    from core.keybackup import open_key_backup, seal_key
    from core.trace_rotate import rotate_traces, verify_traces

    key = b"a" * 32
    other = b"b" * 32
    trace = RunTrace(tmp_path, encrypt_key=key)
    trace(
        SimpleNamespace(
            type="run_started", run_id="tourne", payload={"task": "x"}, seq=1, timestamp=0
        )
    )
    trace(
        SimpleNamespace(
            type="run_finished",
            run_id="tourne",
            payload={"ok": True, "stop_reason": "final"},
            seq=2,
            timestamp=0,
        )
    )
    trace.close()

    report = rotate_traces(tmp_path, key, other)
    assert [item["status"] for item in report] == ["rotated"]
    assert verify_traces(tmp_path, other)[0]["status"] == "verified"

    sealed = seal_key(key, tmp_path / "old.key", "phrase")
    assert open_key_backup(sealed, "phrase") == key


# -- K : reprise -------------------------------------------------------------


def test_resume_plan_never_replays_side_effect_tools(tmp_path: Path) -> None:
    trace = RunTrace(tmp_path)
    trace(
        SimpleNamespace(
            type="run_started",
            run_id="coupe",
            payload={"task": "Ecrire un rapport", "trace_format": 3},
            seq=1,
            timestamp=0,
        )
    )
    trace(
        SimpleNamespace(
            type="tool_started",
            run_id="coupe",
            payload={"tool": "fs_read", "arguments": {"path": "a.txt"}, "step": 1},
            seq=2,
            timestamp=0,
        )
    )
    trace(
        SimpleNamespace(
            type="tool_started",
            run_id="coupe",
            payload={"tool": "fs_write", "arguments": {}, "step": 2},
            seq=3,
            timestamp=0,
        )
    )
    trace.close()

    plan = build_plan(trace.path)

    assert plan.task == "Ecrire un rapport"
    assert len(plan.unsafe) == 1 and "fs_write" in plan.unsafe[0]
    assert "ne pas les refaire" in resume_prompt(plan)


def test_agent_context_is_added_to_the_user_message(tmp_path: Path) -> None:
    from core.agent_core import AgentCore
    from core.llm_client import LLMResponse
    from core.tool_registry import ToolRegistry

    class _Fixed:
        def __init__(self) -> None:
            self.messages: list[list[dict]] = []

        def complete(self, messages, tools=None, **kwargs):
            self.messages.append(list(messages))
            return LLMResponse(text="fini", finish_reason="stop")

        def close(self) -> None:
            pass

    settings = make_settings(tmp_path)
    llm = _Fixed()
    agent = AgentCore(settings, llm, ToolRegistry([]))
    reply = agent.run("continuer", context="etape 1 deja faite")

    assert reply.ok
    assert "etape 1 deja faite" in llm.messages[0][-1]["content"]


# -- M et N : export stable et migration ------------------------------------


def test_export_bundle_validates_and_imports(tmp_path: Path) -> None:
    bundle = {
        "export_format": 1,
        "run_id": "r1",
        "app_version": "1.0",
        "model": "m",
        "ok": True,
        "stop_reason": "final",
        "summary": {"steps": 2},
        "errors": [],
        "steps": [{"index": 1, "calls": [{"tool": "fs_read"}]}],
    }
    assert validate_bundle(bundle) == []
    broken = dict(bundle, export_format=99)
    assert validate_bundle(broken)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    summary = import_bundle(path)
    assert summary["tool_calls"] == 1 and summary["steps"] == 1


def test_v2_trace_is_migrated_with_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "v2.jsonl"
    path.write_text(
        json.dumps({"run_id": "v2", "type": "run_started", "payload": {"trace_format": 2}})
        + "\n",
        encoding="utf-8",
    )
    preview = migrate_file(path, apply=False)
    assert preview["status"] == "would-migrate"
    assert json.loads(path.read_text(encoding="utf-8"))["payload"]["trace_format"] == 2

    done = migrate_file(path, apply=True)
    assert done["status"] == "migrated"
    assert (tmp_path / "v2.jsonl.v2.bak").exists()
    records, _report = read_trace(path)
    assert [record["seq"] for record in records] == [1]
    assert records[0]["payload"]["trace_format"] == 3


def test_benchmark_runs_on_a_small_dataset() -> None:
    from tools.benchmark_traces import run_benchmark

    timings = run_benchmark(runs=200, traces=30, max_seconds=60)

    assert {"seed", "entries", "compact", "rebuild", "doctor"} <= set(timings)
    assert timings["seed"] >= 0
