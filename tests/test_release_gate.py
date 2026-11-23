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
    evaluate_publish_state,
    expected_tag,
    fetch_tag_commit,
    parse_app_version,
    release_exists,
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


def test_un_tag_absent_est_cree_a_la_fin() -> None:
    """Cas normal : rien n'existe, la construction peut aboutir au tag."""
    assert (
        evaluate_publish_state("v1.2.2", "a" * 40, tag_sha=None, has_release=False)
        == "create"
    )


def test_un_tag_sans_release_se_reprend() -> None:
    """Échec après création du tag (téléversement) : on reprend, sans sauter de version."""
    sha = "a" * 40
    assert (
        evaluate_publish_state("v1.2.2", sha, tag_sha=sha, has_release=False)
        == "resume"
    )


def test_un_sha_abrege_designe_le_meme_commit() -> None:
    """`--sha b0b61c6` et le SHA complet rendu par l'API sont le même commit.

    Sans cela, la reprise d'une publication refusait son propre commit : le
    message affichait deux fois le même SHA court.
    """
    sha = "b0b61c6" + "d" * 33
    assert (
        evaluate_publish_state("v1.2.2", "b0b61c6", tag_sha=sha, has_release=False)
        == "resume"
    )


def test_un_tag_sur_un_autre_commit_refuse_la_publication() -> None:
    """La 1.2.0 : le ruleset interdit de déplacer le tag, donc on refuse tôt.

    Accepter reviendrait à publier sous un tag dont le contenu n'est pas celui
    qu'on vient de construire et de vérifier.
    """
    with pytest.raises(GateError) as caught:
        evaluate_publish_state(
            "v1.2.0", "a" * 40, tag_sha="b" * 40, has_release=False
        )
    message = str(caught.value)
    assert "v1.2.0" in message and "b" * 7 in message
    assert "Incrémentez APP_VERSION" in message


def test_une_release_existante_demande_republish() -> None:
    sha = "a" * 40
    with pytest.raises(GateError, match="--republish"):
        evaluate_publish_state("v1.2.1", sha, tag_sha=sha, has_release=True)
    assert (
        evaluate_publish_state(
            "v1.2.1", sha, tag_sha=sha, has_release=True, republish=True
        )
        == "republish"
    )


def _refs(**entries: tuple[int, dict[str, object]]) -> object:
    def get(path: str, _token: str) -> tuple[int, dict[str, object]]:
        for suffix, answer in entries.items():
            if path.endswith(suffix.replace("__", "/")):
                return answer
        return 404, {}

    return get


def test_un_tag_absent_rend_none() -> None:
    get = _refs()
    assert fetch_tag_commit("o/r", "v9.9.9", "t", get=get) is None  # type: ignore[arg-type]


def test_un_tag_annote_est_detache_jusqu_au_commit() -> None:
    """`refs/tags` désigne l'objet `tag` : comparer son SHA refuserait toujours."""

    def get(path: str, _token: str) -> tuple[int, dict[str, object]]:
        if path.endswith("/git/ref/tags/v1.2.2"):
            return 200, {"object": {"sha": "t" * 40, "type": "tag"}}
        if path.endswith("/git/tags/" + "t" * 40):
            return 200, {"object": {"sha": "c" * 40, "type": "commit"}}
        return 404, {}

    assert fetch_tag_commit("o/r", "v1.2.2", "t", get=get) == "c" * 40  # type: ignore[arg-type]


def test_une_release_absente_rend_faux() -> None:
    assert release_exists("o/r", "v9.9.9", "t", get=_refs()) is False  # type: ignore[arg-type]
    present = _refs(**{"releases/tags/v1.2.1": (200, {"tag_name": "v1.2.1"})})
    assert release_exists("o/r", "v1.2.1", "t", get=present) is True  # type: ignore[arg-type]


def test_resolve_commit_reports_a_missing_tag() -> None:
    with tmpdir("release-gate") as tmp:
        with pytest.raises(GateError, match="introuvable"):
            resolve_commit("v999.0.0-inexistant", tmp)


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
