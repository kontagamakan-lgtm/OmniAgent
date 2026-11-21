"""Garde-fous de publication : tag cohérent, commit validé par la CI."""

from __future__ import annotations

import sys

import pytest
from support import ROOT, tmpdir  # noqa: I001

sys.path.insert(0, str(ROOT / "build_smoke"))

from check_release_gate import (  # noqa: E402
    GateError,
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


def test_resolve_commit_reports_a_missing_tag() -> None:
    with tmpdir("release-gate") as tmp:
        with pytest.raises(GateError, match="introuvable"):
            resolve_commit("v999.0.0-inexistant", tmp)


if __name__ == "__main__":
    from support import run

    sys.exit(run(globals()))
