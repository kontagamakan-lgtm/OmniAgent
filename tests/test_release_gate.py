"""Garde-fous de publication : tag cohérent, commit validé par la CI."""

from __future__ import annotations

import sys

import pytest
from support import ROOT, tmpdir  # noqa: I001

sys.path.insert(0, str(ROOT / "build_smoke"))

from check_release_gate import (  # noqa: E402
    GateError,
    await_required_check,
    check_tag_version,
    evaluate_check_runs,
    expected_tag,
    parse_app_version,
    resolve_commit,
)


def test_parse_app_version_reads_the_declaration() -> None:
    text = 'APP_NAME = "OmniAgent"\nAPP_VERSION = "1.2.3"\nOTHER = 1\n'
    assert parse_app_version(text) == "1.2.3"


def test_parse_app_version_rejects_a_config_without_version() -> None:
    with pytest.raises(GateError):
        parse_app_version('APP_NAME = "OmniAgent"\n')


def test_expected_tag_prefixes_with_v() -> None:
    assert expected_tag("1.0.2") == "v1.0.2"


def test_a_matching_tag_passes() -> None:
    assert check_tag_version("v1.0.2", "1.0.2") == "v1.0.2"


def test_a_mismatched_tag_is_refused_with_the_reason() -> None:
    """Publier v1.0.3 avec du contenu 1.0.2 doit échouer, pas passer."""
    with pytest.raises(GateError) as caught:
        check_tag_version("v1.0.3", "1.0.2")
    message = str(caught.value)
    assert "v1.0.3" in message and "1.0.2" in message
    assert "--force-version" in message


def test_a_mismatched_tag_passes_when_explicitly_forced() -> None:
    """Republier un tag historique reste possible, mais explicitement."""
    assert check_tag_version("v1.0.1", "1.0.2", force=True) == "v1.0.2"


def _runs(**states: str) -> dict[str, object]:
    return {
        "check_runs": [
            {"name": name, "status": "completed", "conclusion": state}
            for name, state in states.items()
        ]
    }


def test_a_green_aggregate_check_passes() -> None:
    payload = _runs(**{"Ruff et pytest (Python 3.11)": "success", "Vérifications obligatoires": "success"})
    assert evaluate_check_runs(payload) == "success"


def test_a_failed_aggregate_check_is_refused() -> None:
    with pytest.raises(GateError, match="n'est pas réussi"):
        evaluate_check_runs(_runs(**{"Vérifications obligatoires": "failure"}))


def test_a_running_aggregate_check_is_refused() -> None:
    """Un check encore en cours ne vaut pas une validation."""
    payload = {
        "check_runs": [
            {"name": "Vérifications obligatoires", "status": "in_progress", "conclusion": None}
        ]
    }
    with pytest.raises(GateError, match="n'est pas réussi"):
        evaluate_check_runs(payload)


def test_a_commit_without_the_aggregate_check_is_refused() -> None:
    with pytest.raises(GateError, match="absent"):
        evaluate_check_runs(_runs(**{"Ruff et pytest (Python 3.11)": "success"}))


def test_a_commit_the_ci_never_saw_is_refused() -> None:
    with pytest.raises(GateError, match="jamais vu"):
        evaluate_check_runs({"check_runs": []})


class _FakeClock:
    """Horloge et attente simulées : aucun test n'attend vraiment."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _scripted(*payloads: dict[str, object]):
    """`fetch` qui rend les réponses dans l'ordre, la dernière à satiété."""
    remaining = list(payloads)
    calls: list[dict[str, object]] = []

    def fetch() -> dict[str, object]:
        payload = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        calls.append(payload)
        return payload

    return fetch, calls


def _one(status: str, conclusion: str | None) -> dict[str, object]:
    return {
        "check_runs": [
            {"name": "Vérifications obligatoires", "status": status, "conclusion": conclusion}
        ]
    }


def test_the_gate_waits_for_an_aggregate_check_that_does_not_exist_yet() -> None:
    """Cas de la 1.1.1 : le tag part avant que l'agrégat du merge soit créé."""
    fetch, calls = _scripted(
        _runs(**{"Ruff et pytest (Python 3.11)": "success"}),
        _one("completed", "success"),
    )
    clock = _FakeClock()
    assert await_required_check(fetch, clock=clock, sleep=clock.sleep) == _one(
        "completed", "success"
    )
    assert len(calls) == 2
    assert clock.sleeps == [15.0]


def test_the_gate_waits_while_the_aggregate_check_is_running() -> None:
    fetch, calls = _scripted(_one("in_progress", None), _one("queued", None), _one("completed", "success"))
    clock = _FakeClock()
    await_required_check(fetch, clock=clock, sleep=clock.sleep)
    assert len(calls) == 3
    assert clock.sleeps == [15.0, 15.0]


def test_the_gate_waits_through_an_empty_check_run_list() -> None:
    """Juste après la poussée, l'API peut ne rien connaître du commit."""
    fetch, calls = _scripted({"check_runs": []}, _one("completed", "success"))
    clock = _FakeClock()
    await_required_check(fetch, clock=clock, sleep=clock.sleep)
    assert len(calls) == 2


def test_a_failed_aggregate_check_fails_without_waiting() -> None:
    fetch, calls = _scripted(_one("completed", "failure"))
    clock = _FakeClock()
    with pytest.raises(GateError, match="n'est pas réussi"):
        await_required_check(fetch, clock=clock, sleep=clock.sleep)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_a_cancelled_aggregate_check_fails_without_waiting() -> None:
    fetch, calls = _scripted(_one("completed", "cancelled"))
    clock = _FakeClock()
    with pytest.raises(GateError, match="cancelled"):
        await_required_check(fetch, clock=clock, sleep=clock.sleep)
    assert len(calls) == 1


def test_the_wait_gives_up_after_the_timeout_with_the_last_reason() -> None:
    fetch, calls = _scripted(_runs(**{"Ruff et pytest (Python 3.11)": "success"}))
    clock = _FakeClock()
    with pytest.raises(GateError) as caught:
        await_required_check(fetch, timeout_s=60.0, clock=clock, sleep=clock.sleep)
    message = str(caught.value)
    assert "absent" in message
    assert "5 essais" in message and "--wait-check" in message
    assert len(calls) == 5
    assert clock.now == 60.0


def test_no_wait_still_accepts_a_green_check() -> None:
    fetch, calls = _scripted(_one("completed", "success"))
    clock = _FakeClock()
    await_required_check(fetch, timeout_s=0.0, clock=clock, sleep=clock.sleep)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_resolve_commit_reports_a_missing_tag() -> None:
    with tmpdir("release-gate") as tmp:
        with pytest.raises(GateError, match="introuvable"):
            resolve_commit("v999.0.0-inexistant", tmp)


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
