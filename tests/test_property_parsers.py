"""Tests de propriété : aucun texte arbitraire ne doit faire échouer un lecteur.

Les tests d'exemple couvrent les formes connues ; les parseurs voient des
sorties de modèle et des fichiers coupés dont personne ne connaît la forme.
Hypothesis génère ces entrées et vérifie seulement les garanties du contrat :
le lecteur tolérant rend des dictionnaires, le lint rapporte au lieu de
planter, les parseurs retournent une liste et une chaîne.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given, settings as hypothesis_settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from core.react_parser import final_text, parse_tool_calls  # noqa: E402
from core.trace import TraceFormatError, read_events  # noqa: E402
from core.trace_lint import lint_file  # noqa: E402


@hypothesis_settings(max_examples=40, deadline=None)
@given(
    lines=st.lists(st.text(max_size=80), max_size=6),
    trailing_newline=st.booleans(),
)
def test_the_tolerant_reader_never_fails_on_arbitrary_lines(
    lines: list[str], trailing_newline: bool
) -> None:
    body = "\n".join(lines) + ("\n" if trailing_newline and lines else "")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.jsonl"
        path.write_text(body, encoding="utf-8")

        events = list(read_events(path))

        assert all(isinstance(event, dict) for event in events)
        assert len(events) <= len([line for line in lines if line.strip()])

        issues, report = lint_file(path)
        # `\r`, `\v` et compagnie comptent comme des fins de ligne pour Python.
        assert report.lines <= len(body.splitlines()) + 1
        assert isinstance(issues, list), "le lint rapporte, il ne lève pas"

        # Le mode strict est autorisé à lever un défaut de format, jamais autre chose.
        try:
            strict_events = list(read_events(path, strict=True))
        except (TraceFormatError, UnicodeDecodeError, ValueError, OSError):
            strict_events = None
        if strict_events is not None and strict_events:
            assert len(strict_events) <= len(events)


@hypothesis_settings(max_examples=40, deadline=None)
@given(st.text(max_size=400))
def test_model_output_parsing_is_total(text: str) -> None:
    calls = parse_tool_calls(text)
    cleaned = final_text(text)

    assert isinstance(calls, list)
    assert isinstance(cleaned, str)
