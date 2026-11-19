"""Budget persistant : cumul entre processus, plafonds et arrêt avant requête."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.budget import check, format_state, ledger_path, record_run, usage  # noqa: E402
from core.factory import build_agent  # noqa: E402


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


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
