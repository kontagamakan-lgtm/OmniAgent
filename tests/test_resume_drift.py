"""Dérive des lectures entre la trace et le rejeu de reprise."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import make_settings, tmpdir  # noqa: E402

from config import Settings  # noqa: E402
from core.events import EventType  # noqa: E402
from core.factory import build_agent  # noqa: E402
from core.fingerprint import (  # noqa: E402
    IDENTICAL,
    MISSING,
    MODIFIED,
    compare_fingerprint,
    file_fingerprint,
)
from core.llm_client import LLMResponse  # noqa: E402
from core.react_parser import ParsedToolCall  # noqa: E402
from core.resume import build_plan, replay_read_only, resume_prompt  # noqa: E402
from core.trace import build_trace, read_events  # noqa: E402
from core.trace_lint import lint_file  # noqa: E402

INITIAL = "version initiale du fichier"
UPDATED = "version remplacee par une autre application"


class _ReadOnceLLM:
    """Un tour qui lit un fichier, puis une réponse finale."""

    supports_stream = False

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                text="",
                tool_calls=[
                    ParsedToolCall(
                        name="fs_read_text",
                        arguments={"path": str(self.path)},
                        source="test",
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(text="fini", finish_reason="stop")


def _record_trace(tmp: Path, note: Path) -> tuple[Settings, Path]:
    """Exécute un vrai tour de lecture et retourne (settings, fichier de trace)."""
    settings = make_settings(tmp)
    bundle = build_agent(
        settings, llm=_ReadOnceLLM(note), with_web=False, with_browser=False
    )
    trace = build_trace(settings)
    unsubscribe = bundle.agent.bus.subscribe(trace)
    try:
        bundle.agent.run("lis la note")
    finally:
        unsubscribe()
        trace.close()
        bundle.close()
    files = sorted((settings.paths.log_dir / "runs").glob("*.jsonl"))
    assert files, "aucune trace écrite par le run de lecture"
    return settings, files[-1]


def test_trace_records_a_light_fingerprint_of_files_read() -> None:
    with tmpdir("empreinte") as tmp:
        note = tmp / "note.txt"
        note.write_text(INITIAL, encoding="utf-8")
        _settings, trace_path = _record_trace(tmp, note)
        records = list(read_events(trace_path))
        raw_trace = trace_path.read_text(encoding="utf-8")
        issues, _report = lint_file(trace_path)

    started = [
        record for record in records if record["type"] == EventType.TOOL_STARTED
    ]
    assert len(started) == 1, "la lecture doit produire un seul tool_started"
    fingerprint = started[0]["payload"]["fingerprint"]
    expected = hashlib.sha256(INITIAL.encode("utf-8")).hexdigest()
    assert fingerprint["path"] == str(note.resolve())
    assert fingerprint["size"] == len(INITIAL.encode("utf-8"))
    assert fingerprint["head_sha256"] == expected
    # Empreinte légère : le contenu lu n'est pas recopié dans la trace.
    assert INITIAL not in raw_trace
    # Le champ ajouté ne casse pas le contrat de format vérifié par le linter.
    assert not [issue for issue in issues if issue.error]


def test_resume_reports_a_file_modified_since_the_trace() -> None:
    with tmpdir("derive") as tmp:
        note = tmp / "note.txt"
        note.write_text(INITIAL, encoding="utf-8")
        settings, trace_path = _record_trace(tmp, note)

        # Le fichier change entre la trace et la reprise : c'est le cas à couvrir.
        note.write_text(UPDATED, encoding="utf-8")
        bundle = build_agent(settings, with_web=False, with_browser=False)
        try:
            plan = build_plan(trace_path)
            assert plan.calls, "la trace doit contenir l'appel de lecture"
            assert plan.calls[0].fingerprint is not None
            replayed = replay_read_only(plan, bundle.registry)
        finally:
            bundle.close()

        observation = replayed.executed[0]
        prompt = resume_prompt(plan, replay=replayed)

    assert observation.ok, observation.text
    assert observation.drift == MODIFIED
    assert observation.drift_label == "contenu modifie depuis la trace"
    assert UPDATED in observation.text, "l'observation doit être le contenu frais"
    assert "contenu modifie depuis la trace" in prompt
    assert "Derive depuis la trace" in prompt
    assert UPDATED in prompt
    # Le contenu d'origine n'est nulle part : le modèle ne peut pas s'y fier.
    assert INITIAL not in prompt


def test_fingerprint_comparison_distinguishes_identical_modified_missing() -> None:
    with tmpdir("comparaison") as tmp:
        note = tmp / "note.txt"
        note.write_text(INITIAL, encoding="utf-8")
        recorded = file_fingerprint(note)
        assert recorded is not None

        assert compare_fingerprint(recorded)[0] == IDENTICAL
        note.write_text(UPDATED, encoding="utf-8")
        assert compare_fingerprint(recorded)[0] == MODIFIED
        note.unlink()
        assert compare_fingerprint(recorded)[0] == MISSING

    # Trace antérieure, sans empreinte : aucune conclusion plutôt qu'une supposition.
    assert compare_fingerprint(None) == ("", None)
    assert compare_fingerprint({"path": ""}) == ("", None)
