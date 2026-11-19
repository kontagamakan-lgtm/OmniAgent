"""Rattrapage des fenêtres manquées, sans horloge ni réveil réel."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from core.autonomy import goals_path  # noqa: E402
from core.schedule import (  # noqa: E402
    Schedule,
    load_schedule,
    missed_slot,
    plan_start,
    run_scheduled,
    schedule_path,
    sessions_path,
    should_start,
)


def test_a_missed_window_is_caught_up_once() -> None:
    schedule = Schedule(start_hour=1, end_hour=6)
    now = datetime(2026, 1, 2, 10, 0)

    decision = plan_start(schedule, now, idle_s=600, on_battery=False)

    assert decision.run is True
    assert decision.catch_up is True
    assert decision.slot == "2026-01-02"
    assert "rattrapage" in decision.reason

    # La fenêtre rattrapée est enregistrée : le verrou la refuse ensuite.
    again = plan_start(
        schedule, now, idle_s=600, on_battery=False, last_slot=decision.slot
    )
    assert again.run is False
    assert "deja faite" in again.reason


def test_an_in_window_start_is_not_a_catch_up() -> None:
    schedule = Schedule(start_hour=1, end_hour=6)
    decision = plan_start(
        schedule, datetime(2026, 1, 2, 2, 0), idle_s=600, on_battery=False
    )

    assert decision.run is True
    assert decision.catch_up is False
    assert decision.slot == "2026-01-02"
    assert decision.reason == "conditions reunies"


def test_an_old_missed_window_is_abandoned() -> None:
    # Fenêtre 8-12 : la dernière s'est terminée hier à midi. À 7 h le
    # lendemain, le rattrapage aurait 19 h de retard (plafond 18 h).
    schedule = Schedule(start_hour=8, end_hour=12, catch_up_hours=18.0)
    decision = plan_start(
        schedule, datetime(2026, 1, 3, 7, 0), idle_s=600, on_battery=False
    )

    assert decision.run is False
    assert "trop ancienne" in decision.reason
    assert "19.0 h" in decision.reason


def test_catch_up_respects_policy_and_machine_conditions() -> None:
    now = datetime(2026, 1, 2, 10, 0)
    disabled = Schedule(start_hour=1, end_hour=6, catch_up="none")
    refused = plan_start(disabled, now, idle_s=600, on_battery=False)
    assert refused.run is False and "hors fenetre" in refused.reason

    active = plan_start(Schedule(), now, idle_s=10, on_battery=False)
    assert active.run is False and "machine active" in active.reason

    battery = plan_start(Schedule(), now, idle_s=600, on_battery=True)
    assert battery.run is False and "batterie" in battery.reason


def test_an_overnight_window_is_located_across_midnight() -> None:
    schedule = Schedule(start_hour=22, end_hour=6)

    # 1 h : la fenêtre commencée la veille à 22 h est encore ouverte, donc pas
    # manquée ; la dernière *terminée* est celle d'avant.
    assert missed_slot(schedule, datetime(2026, 1, 3, 1, 0)) == "2026-01-01"
    # 10 h : la fenêtre 22 h -> 6 h est finie, c'est elle qu'on rattrape.
    assert missed_slot(schedule, datetime(2026, 1, 3, 10, 0)) == "2026-01-02"
    assert should_start(
        schedule,
        datetime(2026, 1, 3, 10, 0),
        idle_s=600,
        on_battery=False,
    )[0]


def test_schedule_file_parses_the_catch_up_policy() -> None:
    with tmpdir("rattrapage-config") as tmp:
        settings = make_settings(tmp)
        target = schedule_path(settings)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"catch_up": "none", "catch_up_hours": 6}), encoding="utf-8"
        )
        schedule = load_schedule(settings)
        assert schedule.catch_up == "none"
        assert schedule.catch_up_hours == 6.0

        target.write_text(json.dumps({"catch_up": "jamais"}), encoding="utf-8")
        assert load_schedule(settings).catch_up == "latest"


class _Reply:
    ok = True
    text = "session faite"
    stop_reason = "final"
    error = ""


class _Agent:
    """Agent factice : compte les sessions réellement lancées."""

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


def test_the_runner_records_the_caught_up_window_and_never_repeats_it() -> None:
    with tmpdir("rattrapage-runner") as tmp:
        settings = make_settings(tmp)
        settings.autonomy.max_cycles = 1
        settings.autonomy.notify_on_session = False
        goals = goals_path(settings)
        goals.parent.mkdir(parents=True, exist_ok=True)
        goals.write_text("- ranger les rapports\n", encoding="utf-8")
        agent = _Agent()
        moments = [datetime(2026, 1, 2, 10, 0)]

        first = run_scheduled(
            settings,
            agent,
            max_checks=1,
            now=lambda: moments[0],
            idle=lambda: 600.0,
            on_battery=lambda: False,
            sleep=lambda _seconds: None,
        )
        records = [
            json.loads(line)
            for line in sessions_path(settings).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # Un second démarrage dans la même fenêtre, même hors horaire.
        second = run_scheduled(
            settings,
            agent,
            max_checks=1,
            now=lambda: moments[0],
            idle=lambda: 600.0,
            on_battery=lambda: False,
            sleep=lambda _seconds: None,
        )

    assert len(first) == 1
    assert first[0]["slot"] == "2026-01-02"
    assert first[0]["catch_up"] is True
    assert records[-1]["slot"] == "2026-01-02"
    assert records[-1]["catch_up"] is True
    assert len(agent.tasks) == 1, "la fenêtre rattrapée ne doit pas repartir"
    assert second == []
