"""Budget persistant : cumul entre processus, plafonds et arrêt avant requête."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.agent_core import AgentCore  # noqa: E402
from core.budget import (  # noqa: E402
    check,
    estimate_run,
    format_state,
    ledger_path,
    record_run,
    release_run,
    reserve_run,
    run_cost,
    usage,
)
from core.factory import build_agent  # noqa: E402
from core.llm_client import LLMResponse  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402


class _ExplodingLLM:
    """Aucun appel ne doit partir si le budget est déjà épuisé."""

    supports_stream = False

    def complete(self, messages, tools=None, **kwargs):  # pragma: no cover
        raise AssertionError("le budget devait arrêter le run avant le fournisseur")


def test_usage_splits_day_week_month_and_ignores_old_entries() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    entries = [
        {"at": now.isoformat(), "cost": 0.20, "tokens": 100},
        {"at": (now - timedelta(days=1)).isoformat(), "cost": 0.30, "tokens": 200},
        {"at": (now - timedelta(days=10)).isoformat(), "cost": 0.50, "tokens": 300},
        {"at": "illisible", "cost": 9.0, "tokens": 9},
    ]
    result = usage(entries, now)
    assert result.day_cost == 0.20 and result.day_tokens == 100
    assert result.week_cost == 0.50
    assert result.month_cost == 1.00 and result.month_tokens == 600
    assert result.runs_month == 3
    assert result.month_average_run_cost == 0.3333


def test_daily_cap_refuses_before_exceeding() -> None:
    with tmpdir("budget-plafond") as tmp:
        settings = make_settings(tmp)
        settings.budget.daily_cost = 1.0
        assert check(settings).allowed
        record_run(settings, run_id="r1", model="gpt-4o-mini", prompt_tokens=0, completion_tokens=0)
        # Le registre persiste : la décision suivante le relit.
        allowed = check(settings, pending_cost=0.60)
        assert allowed.allowed
        now = datetime.now(tz=timezone.utc)
        entries = [
            {"at": now.isoformat(), "cost": 0.95, "tokens": 10},
        ]
        ledger_path(settings).write_text(
            "\n".join(__import__("json").dumps(item) for item in entries) + "\n",
            encoding="utf-8",
        )
        blocked = check(settings, pending_cost=0.10)
        assert not blocked.allowed and "jour" in blocked.reason
        assert "budget" in format_state(blocked)
        assert blocked.remaining < 0.1


def test_unreadable_ledger_refuses_instead_of_reporting_zero() -> None:
    with tmpdir("budget-illisible") as tmp:
        settings = make_settings(tmp)
        settings.budget.enabled = True
        ledger = ledger_path(settings)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        # Un vrai ledger.jsonl avec des octets UTF-8 invalides : read_text()
        # échoue sans dépendre des permissions du compte de test.
        ledger.write_bytes(b"\xff\xfe{\"run_id\": \"illisible\"}\n")
        assert ledger.name == "ledger.jsonl"

        state = check(settings)
        assert not state.allowed
        assert "budget indisponible" in state.reason

        allowed, reason, reservation = reserve_run(
            settings, run_id="registre-illisible", cost=0.01, tokens=1
        )
        assert not allowed and "budget indisponible" in reason
        assert reservation == ""
        assert record_run(
            settings,
            run_id="registre-illisible",
            model=settings.llm.model,
            prompt_tokens=1,
            completion_tokens=1,
        ) is None


def test_corrupt_json_ledger_refuses_instead_of_reporting_zero() -> None:
    with tmpdir("budget-json-casse") as tmp:
        settings = make_settings(tmp)
        settings.budget.enabled = True
        settings.budget.daily_cost = 1.0
        ledger = ledger_path(settings)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        # Une ligne JSON invalide ne doit pas être ignorée comme une dépense
        # nulle : le contrôle autoritaire refuse tout le registre.
        ledger.write_text("{ceci n'est pas du JSON}\n", encoding="utf-8")

        state = check(settings)
        assert not state.allowed
        assert "budget indisponible" in state.reason
        assert state.usage.runs_today == 0 and state.usage.day_cost == 0.0

        allowed, reason, reservation = reserve_run(
            settings, run_id="json-casse", cost=0.01, tokens=1
        )
        assert not allowed and "budget indisponible" in reason
        assert reservation == ""
        assert record_run(
            settings,
            run_id="json-casse",
            model=settings.llm.model,
            prompt_tokens=1,
            completion_tokens=1,
        ) is None


def test_ledger_survives_a_new_process() -> None:
    with tmpdir("budget-processus") as tmp:
        settings = make_settings(tmp)
        settings.budget.daily_cost = 5.0
        record_run(settings, run_id="r1", model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500)

        code = (
            "import sys; sys.path.insert(0, 'tests');"
            "from pathlib import Path;"
            "from support import make_settings;"
            "from core.budget import check, record_run;"
            "s = make_settings(Path(sys.argv[1]));"
            "record_run(s, run_id='r2', model='gpt-4o-mini', prompt_tokens=2000, completion_tokens=1000);"
            "print(check(s).usage.runs_today, check(s).usage.day_tokens)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code, str(tmp)],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        runs, tokens = completed.stdout.split()
        assert int(runs) == 2 and int(tokens) == 4500


def test_agent_refuses_to_start_when_the_cap_is_already_reached() -> None:
    with tmpdir("budget-agent") as tmp:
        settings = make_settings(tmp)
        settings.budget.daily_cost = 0.01
        record_run(
            settings,
            run_id="hier",
            model=settings.llm.model,
            prompt_tokens=10_000_000,
            completion_tokens=0,
        )
        bundle = build_agent(
            settings, llm=_ExplodingLLM(), with_web=False, with_browser=False
        )
        try:
            reply = bundle.agent.run("coute trop cher")
        finally:
            bundle.close()
        assert reply.ok is False
        assert reply.stop_reason == "budget"
        assert reply.error_code == "budget_exceeded"
        assert "budget" in reply.text


def test_unlimited_budget_still_records_the_gauge() -> None:
    with tmpdir("budget-jauge") as tmp:
        settings = make_settings(tmp)
        assert check(settings).remaining == float("inf")
        record_run(settings, run_id="r1", model="gpt-4o-mini", prompt_tokens=10, completion_tokens=5)
        state = check(settings)
        assert state.allowed
        assert state.usage.runs_today == 1
        assert "Aucun plafond" in format_state(state)


def test_two_concurrent_reservations_cannot_both_pass_the_cap() -> None:
    """Lecture + contrôle + écriture sous un seul verrou : un seul passe."""
    with tmpdir("budget-reservations") as tmp:
        settings = make_settings(tmp)
        settings.budget.daily_cost = 1.0
        barrier = threading.Barrier(2)
        results: list[tuple[bool, str, str]] = []
        lock = threading.Lock()

        def attempt(run_id: str) -> None:
            barrier.wait(timeout=5)
            outcome = reserve_run(settings, run_id=run_id, cost=0.6, tokens=100)
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=attempt, args=(name,)) for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        allowed = [item for item in results if item[0]]
        refused = [item for item in results if not item[0]]
        assert len(allowed) == 1, results
        assert len(refused) == 1 and "budget jour" in refused[0][1], results
        winner = allowed[0][2]
        assert release_run(settings, winner)
        # Après libération, le second run peut réserver à son tour.
        retry = reserve_run(settings, run_id="c", cost=0.6, tokens=100)
        assert retry[0] is True, retry
        assert release_run(settings, retry[2])


def test_average_past_run_blocks_the_second_underestimated_reservation() -> None:
    """Une moyenne multi-étapes réserve assez pour que le second run soit refusé."""
    with tmpdir("budget-estimation-moyenne") as tmp:
        settings = make_settings(tmp)
        settings.llm.price_completion_per_million = 1000.0
        settings.llm.max_output_tokens = 512
        settings.budget.daily_cost = 6.0
        # Run multi-étapes passé : 3000 tokens de sortie = 3 USD.
        record_run(
            settings,
            run_id="run-multi-etapes",
            model=settings.llm.model,
            prompt_tokens=0,
            completion_tokens=3000,
        )

        cost, tokens = estimate_run(settings, prompt_tokens=0)
        assert cost >= 3.0, cost
        assert tokens >= 3000, tokens

        settings.budget.estimate_multiplier = 2.0
        doubled_cost, doubled_tokens = estimate_run(settings, prompt_tokens=0)
        assert doubled_cost >= cost * 2, doubled_cost
        assert doubled_tokens >= tokens * 2, doubled_tokens
        settings.budget.estimate_multiplier = 1.0

        barrier = threading.Barrier(2)
        results: list[tuple[bool, str, str]] = []
        lock = threading.Lock()

        def attempt(run_id: str) -> None:
            barrier.wait(timeout=5)
            outcome = reserve_run(settings, run_id=run_id, cost=cost, tokens=tokens)
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=attempt, args=(name,)) for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        allowed = [item for item in results if item[0]]
        refused = [item for item in results if not item[0]]
        assert len(allowed) == 1, results
        assert len(refused) == 1 and "budget jour" in refused[0][1], results
        assert release_run(settings, allowed[0][2])


def test_legacy_underestimate_would_allow_both_runs_to_overrun() -> None:
    """Sans moyenne, deux réservations passent, puis le coût réel dépasse le cap."""
    with tmpdir("budget-ancienne-estimation") as tmp:
        settings = make_settings(tmp)
        settings.llm.price_completion_per_million = 1000.0
        settings.llm.max_output_tokens = 512
        settings.budget.daily_cost = 6.0
        # Run multi-étapes passé : 3000 tokens de sortie = 3 USD.
        assert record_run(
            settings,
            run_id="run-multi-etapes",
            model=settings.llm.model,
            prompt_tokens=0,
            completion_tokens=3000,
        ) is not None

        # Ancien calcul : un seul appel maximal (0,512 USD), qui ignore le coût
        # réel du run multi-étapes. La moyenne historique vaut 3 USD.
        legacy_cost = run_cost(settings, 0, settings.llm.max_output_tokens)
        average_cost, average_tokens = estimate_run(settings, prompt_tokens=0)
        assert legacy_cost < average_cost, (legacy_cost, average_cost)
        assert average_cost >= 3.0 and average_tokens >= 3000

        old_runs = ("old-a", "old-b")
        reservations = [
            reserve_run(
                settings,
                run_id=run_id,
                cost=legacy_cost,
                tokens=settings.llm.max_output_tokens,
            )
            for run_id in old_runs
        ]
        assert all(allowed for allowed, _reason, _reservation in reservations), reservations

        # Les deux runs terminent au coût réel multi-étapes (3 USD chacun),
        # sans que l'ancienne estimation ne les ait empêchés de démarrer.
        for run_id in old_runs:
            assert record_run(
                settings,
                run_id=run_id,
                model=settings.llm.model,
                prompt_tokens=0,
                completion_tokens=3000,
            ) is not None

        state = check(settings)
        assert state.usage.day_cost > settings.budget.daily_cost, state.usage
        assert not state.allowed and "jour" in state.reason, state.reason


class _GatedLLM:
    """Un seul appel part ; il attend que le second run ait tenté sa chance."""

    supports_stream = False

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return LLMResponse(text="fini", finish_reason="stop")


def test_two_concurrent_agent_runs_do_not_exceed_the_cap() -> None:
    """Le second run est refusé avant l'appel modèle, pas après facturation."""
    with tmpdir("budget-runs-concurrents") as tmp:
        settings = make_settings(tmp)
        # Estimation = 4096 tokens de sortie * 1000 USD / million = 4,096 USD.
        settings.llm.price_completion_per_million = 1000.0
        settings.llm.max_output_tokens = 4096
        settings.budget.daily_cost = 5.0

        llm = _GatedLLM()
        first = AgentCore(settings, llm, ToolRegistry([]))
        second = AgentCore(settings, llm, ToolRegistry([]))
        replies: dict[str, object] = {}

        def run(agent: AgentCore, label: str) -> None:
            replies[label] = agent.run("bonjour")

        first_thread = threading.Thread(target=run, args=(first, "first"))
        first_thread.start()
        assert llm.entered.wait(timeout=5), "le premier run doit réserver puis appeler"

        second_thread = threading.Thread(target=run, args=(second, "second"))
        second_thread.start()
        second_thread.join(timeout=10)
        assert not second_thread.is_alive()
        assert replies["second"].stop_reason == "budget", replies["second"]
        assert llm.calls == 1, "le run refusé ne doit jamais atteindre le modèle"

        llm.release.set()
        first_thread.join(timeout=10)
        assert not first_thread.is_alive()
        assert replies["first"].ok is True, replies["first"]

        entries = [
            __import__("json").loads(line)
            for line in ledger_path(settings).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        actual = [entry for entry in entries if entry.get("kind") != "reservation"]
        assert len(actual) == 1, entries
        assert not [entry for entry in entries if entry.get("kind") == "reservation"]
        assert float(actual[0]["cost"]) <= 5.0, actual[0]
        assert usage(entries).day_cost <= 5.0


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
