"""Tests du point d'entrée console (`app.py --task`)."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

import app
from core.factory import build_agent
from core.events import Event, EventType
from core.llm_client import LLMResponse, ParsedToolCall
from core.prompts import FINAL_ANSWER_PREFIX


def test_task_writes_a_jsonl_trace_per_run() -> None:
    """`--task` doit laisser la trace que l'utilisateur pourra relire."""

    class _LLM:
        def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(text="terminé", finish_reason="stop")

    with tmpdir("app") as tmp:
        settings = make_settings(tmp)
        args = argparse.Namespace(task="trace", no_browser=True, unlock_keyring=False)
        original = app.build_agent

        def _offline(settings, **options):
            options.update(llm=_LLM(), with_web=False, with_browser=False)
            return original(settings, **options)

        app.build_agent = _offline
        try:
            code = app._run_task(settings, args)
        finally:
            app.build_agent = original

        assert code == 0
        files = sorted((settings.paths.log_dir / "runs").glob("*.jsonl"))
        assert files, "aucune trace écrite par --task"
        text = files[-1].read_text(encoding="utf-8")
        assert '"type": "run_started"' in text
        assert '"type": "run_finished"' in text


def test_console_renders_the_context_trim_report() -> None:
    """Le repli silencieux est terminé : la console l'explique."""
    out = io.StringIO()
    stream = app._ConsoleStream(out)

    stream(
        Event(
            type=EventType.CONTEXT_TRIMMED,
            payload={
                "before_messages": 12,
                "after_messages": 7,
                "before_tokens": 900,
                "after_tokens": 420,
                "summarized": 5,
            },
        )
    )

    text = out.getvalue()
    assert "contexte replié" in text
    assert "12 -> 7 messages" in text
    assert "900 -> 420 tokens" in text
    assert "5 résumé(s)" in text


class _Recorder:
    """Capture les écritures et note si `agent.run` était déjà revenu."""

    def __init__(self) -> None:
        self.parts: list[tuple[str, bool]] = []
        self.run_done = False

    def write(self, text: str) -> int:
        self.parts.append((text, self.run_done))
        return len(text)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return "".join(part for part, _ in self.parts)


class _StreamingLLM:
    """Fournisseur qui débite sa réponse en fragments."""

    supports_stream = True

    def __init__(self, chunks: list[str], text: str) -> None:
        self.chunks = chunks
        self.text = text

    def stream_complete(self, messages, tools=None, *, on_token=None, **kwargs):
        for chunk in self.chunks:
            if on_token is not None:
                on_token(chunk)
        return LLMResponse(text=self.text)


class _BlockingLLM:
    """Fournisseur sans flux : une seule réponse complète."""

    supports_stream = False

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, messages, tools=None, **kwargs):
        return LLMResponse(text=self.text)


class _ChattyLLM:
    """Premier tour : préambule + appel d'outil ; second tour : la réponse."""

    supports_stream = True

    def __init__(self, note: Path) -> None:
        self.note = note
        self.calls = 0

    def stream_complete(self, messages, tools=None, *, on_token=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            for chunk in ("Je vais ", "lire la note."):
                if on_token is not None:
                    on_token(chunk)
            return LLMResponse(
                text="Je vais lire la note.",
                tool_calls=[
                    ParsedToolCall(
                        name="fs_read_text",
                        arguments={"path": str(self.note)},
                        source="test",
                    )
                ],
                finish_reason="tool_calls",
            )
        for chunk in ("Voici ", "la réponse."):
            if on_token is not None:
                on_token(chunk)
        return LLMResponse(text=f"{FINAL_ANSWER_PREFIX} Voici la réponse.")


def _run(monkeypatch, tmp, llm) -> tuple[int, _Recorder]:
    settings = make_settings(tmp)
    settings.llm.stream = True
    bundle = build_agent(settings, llm=llm, with_web=False, with_browser=False)
    monkeypatch.setattr(app, "build_agent", lambda *a, **k: bundle)
    recorder = _Recorder()
    monkeypatch.setattr(sys, "stdout", recorder)

    original = bundle.agent.run

    def run_then_mark(task):
        try:
            return original(task)
        finally:
            recorder.run_done = True

    bundle.agent.run = run_then_mark  # type: ignore[method-assign]

    args = argparse.Namespace(task="dis bonjour", no_browser=True, unlock_keyring=False)
    code = app._run_task(settings, args)
    return code, recorder


def test_task_prints_stream_fragments_as_they_arrive(monkeypatch) -> None:
    """Les fragments sortent pendant l'exécution, dans l'ordre, une seule fois."""
    with tmpdir("app") as tmp:
        llm = _StreamingLLM(["Bon", "jour", " !"], f"{FINAL_ANSWER_PREFIX} Bonjour !")

        code, recorder = _run(monkeypatch, tmp, llm)

        assert code == 0
        assert [text for text, _ in recorder.parts] == ["Bon", "jour", " !", "\n"]
        # Les trois fragments précèdent la fin de l'exécution ; seul le saut de
        # ligne final est écrit après.
        assert [done for _, done in recorder.parts] == [False, False, False, True]
        assert recorder.text() == "Bonjour !\n"
        assert recorder.text().count("Bonjour") == 1, "la réponse a été réimprimée"


def test_task_hides_intermediate_text_and_keeps_only_the_final_answer(monkeypatch) -> None:
    """Le préambule d'un tour qui appelle un outil n'est pas imprimé."""
    with tmpdir("app") as tmp:
        note = tmp / "note.txt"
        note.write_text("contenu", encoding="utf-8")
        llm = _ChattyLLM(note)

        code, recorder = _run(monkeypatch, tmp, llm)

        assert code == 0
        assert llm.calls == 2, "l'outil n'a pas été appelé puis suivi d'une réponse"
        assert recorder.text() == "Voici la réponse.\n"
        assert "Je vais" not in recorder.text(), "le texte intermédiaire a fuité"
        # La réponse finale reste diffusée pendant l'exécution, fragment par fragment.
        assert recorder.parts[0] == ("Voici ", False)
        assert [text for text, _ in recorder.parts] == ["Voici ", "la réponse.", "\n"]


def test_task_prints_the_whole_answer_once_without_streaming(monkeypatch) -> None:
    """Sans flux, la réponse complète reste affichée une seule fois."""
    with tmpdir("app") as tmp:
        llm = _BlockingLLM(f"{FINAL_ANSWER_PREFIX} Bonjour !")

        code, recorder = _run(monkeypatch, tmp, llm)

        assert code == 0
        assert recorder.text() == "Bonjour !\n"
        assert all(done for _, done in recorder.parts)


def test_console_stream_drops_the_abandoned_fragment_after_a_retry() -> None:
    """En console aussi, un flux coupé ne rejoint pas la réponse qui le suit."""
    out = io.StringIO()
    stream = app._ConsoleStream(out)
    stream(Event(type=EventType.STREAM_CHUNK, payload={"step": 1, "text": "Bonjour"}))
    stream(Event(type=EventType.LLM_RETRY, payload={"attempt": 1, "code": "llm_error"}))
    stream(Event(type=EventType.STREAM_CHUNK, payload={"step": 1, "text": "Salut"}))
    stream(Event(type=EventType.FINAL, payload={"step": 1, "text": "Salut"}))

    written = out.getvalue()
    assert "Salut" in written, written
    assert "Bonjour" not in written, written
    assert "reprise 1" in written, written
    assert stream.covers("Salut")


def test_check_lists_the_tesseract_hint_when_the_binary_is_missing(monkeypatch) -> None:
    """Binaire absent : `--check` donne le lien, la clé et la vérification."""
    with tmpdir("app") as tmp:
        settings = make_settings(tmp)

        from tools.ocr import Ocr

        monkeypatch.setattr(
            Ocr,
            "probe",
            staticmethod(lambda executable=None: (False, "binaire Tesseract introuvable")),
        )

        lines = [line for ok, line in app.diagnostics(settings) if line.startswith("OCR - ")]
        text = "\n".join(lines)

        assert len(lines) == 3, lines
        assert "https://github.com/UB-Mannheim/tesseract/wiki" in text, text
        assert "files.tesseract_cmd" in text, text
        assert str(settings.paths.config_file) in text, text
        assert 'C:\\Program Files\\Tesseract-OCR\\tesseract.exe' in text, text
        assert "--version" in text, text
        assert "python app.py --check" in text, text


def test_diagnostics_reports_the_ocr_state_without_blocking() -> None:
    """L'OCR est signalé avec sa cause précise, et reste optionnel."""
    with tmpdir("app") as tmp:
        settings = make_settings(tmp)
        checks = app.diagnostics(settings)

        ocr = [(ok, line) for ok, line in checks if line.startswith("OCR :")]
        assert len(ocr) == 1, checks
        ok, line = ocr[0]
        assert ok is True, "l'OCR ne doit jamais bloquer le démarrage"

        from tools.ocr import Ocr

        ready, reason = Ocr.probe(settings.files.tesseract_cmd or "")
        assert reason in line, (line, reason)
        # « indisponible » contient « disponible » : on compare le préfixe exact.
        assert line.startswith("OCR : disponible" if ready else "OCR : indisponible"), line
