"""Golden traces : comparaison exacte et invariants critiques explicites."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import golden_traces as harness  # noqa: E402


@pytest.mark.parametrize("name", list(harness.SCENARIOS))
def test_scenario_matches_its_golden(name: str) -> None:
    """Trace normalisée et effets observés doivent égaler le golden commité."""
    actual = harness.SCENARIOS[name]()
    expected = harness.load_golden(name)
    assert actual == expected, harness.explain(name, expected, actual)


def test_the_golden_directory_matches_the_scenarios() -> None:
    present = {path.stem for path in harness.GOLDEN_DIR.glob("*.json")}
    assert present == set(harness.SCENARIOS), (
        "golden orphelin, ou scénario sans golden : "
        f"présents {sorted(present)}, attendus {sorted(harness.SCENARIOS)}"
    )


def test_the_harness_is_deterministic() -> None:
    """Deux exécutions du même scénario donnent exactement les mêmes faits."""
    assert harness.SCENARIOS["budget_exhausted"]() == harness.SCENARIOS[
        "budget_exhausted"
    ]()


def test_critical_effects_are_asserted_directly() -> None:
    """Les invariants restent lisibles même si un golden était régénéré à tort."""
    resume = harness.SCENARIOS["resume_no_effect_replay"]()["effects"]
    assert resume["dispatched"] == ["fs_read_text"]
    assert resume["refused"] == ["fs_write_text"]
    assert resume["content_after_replay"] == "v2"
    assert resume["backups_created"] == 0
    assert resume["drift"] == ["modified"]

    approval = harness.SCENARIOS["proposal_approval"]()["effects"]
    assert approval["stored_state"] == "executed"
    assert approval["run_id_matches"] is True
    assert approval["dispatched"] == ["fs_write_text"]
    assert approval["report_written"] == "rapport"
    assert approval["pending_left"] == 0

    cancelled = harness.SCENARIOS["cancel"]()["effects"]
    assert cancelled["dispatched"] == ["fs_write_text"]
    assert cancelled["second_file"] is False
    assert cancelled["provider_requests"] == 1

    budget = harness.SCENARIOS["budget_exhausted"]()["effects"]
    assert budget["dispatched"] == ["fs_write_text", "fs_write_text"]
    assert budget["files"] == ["a.txt", "b.txt"]
    assert budget["closing_without_tools"] is True

    schedule = harness.SCENARIOS["scheduled_slot_already_honored"]()["effects"]
    assert schedule["runs"] == 0
    assert schedule["session_lines"] == 1

    secret = harness.SCENARIOS["prompt_without_secret"]()["effects"]
    assert secret["secret_in_prompts"] is False
    assert secret["secret_in_trace"] is False
    assert secret["secret_in_vault_file"] is False
    assert secret["sensitive_observation"] is True
