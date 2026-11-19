"""Propriétés du masquage : aucun secret généré ne doit ressortir.

Les tests d'exemple vérifient des chaînes connues ; une clé concaténée à un
préfixe, un JWT généré ou un numéro de carte groupé différemment peuvent
passer entre les mailles. Hypothesis génère ces formes et l'invariant est
simple : la valeur secrète ne doit plus apparaître dans le texte masqué.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given, settings as hypothesis_settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from core.events import Event, EventType  # noqa: E402
from core.trace import RunTrace, read_events  # noqa: E402
from security.redaction import redact  # noqa: E402

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_TOKEN = st.text(alphabet=_ALNUM + "_-", min_size=16, max_size=48)


@hypothesis_settings(max_examples=40, deadline=None)
@given(suffix=_TOKEN)
def test_provider_keys_never_survive_redaction(suffix: str) -> None:
    secret = "sk-" + suffix
    assert secret not in redact(f"clé = {secret} fin")


@hypothesis_settings(max_examples=30, deadline=None)
@given(
    header=st.text(alphabet=_ALNUM, min_size=8, max_size=24),
    payload=st.text(alphabet=_ALNUM, min_size=8, max_size=24),
    signature=st.text(alphabet=_ALNUM + "_-", min_size=8, max_size=24),
)
def test_jwt_never_survives_redaction(header: str, payload: str, signature: str) -> None:
    token = f"eyJ{header}.{payload}.{signature}"
    assert token not in redact(f"jeton {token}")


@hypothesis_settings(max_examples=30, deadline=None)
@given(token=_TOKEN)
def test_authorization_header_is_masked(token: str) -> None:
    assert token not in redact(f"Authorization: Bearer {token}")


@hypothesis_settings(max_examples=20, deadline=None)
@given(groups=st.lists(st.integers(0, 9999), min_size=4, max_size=4))
def test_grouped_card_numbers_are_masked(groups: list[int]) -> None:
    digits = [f"{value:04d}" for value in groups]
    number = " ".join(digits)
    assert number not in redact(f"carte {number} expire bientot")


def test_a_trace_never_contains_a_redacted_secret(tmp_path: Path) -> None:
    trace = RunTrace(tmp_path)
    secret = "sk-test-0123456789abcdef"
    trace(
        Event(
            type=EventType.RUN_STARTED,
            run_id="secret-run",
            payload={"task": f"utilise {secret}", "model": "m"},
            seq=1,
        )
    )
    trace(
        Event(
            type=EventType.RUN_FINISHED,
            run_id="secret-run",
            payload={"ok": True, "stop_reason": "final", "authorization": secret},
            seq=2,
        )
    )
    trace.close()
    body = trace.path.read_text(encoding="utf-8")
    assert secret not in body
    events = list(read_events(trace.path))
    assert events and "sk-test" not in json.dumps(events, ensure_ascii=False)
