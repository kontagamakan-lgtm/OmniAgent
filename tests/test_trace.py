"""Trace JSONL : un fichier par exécution, payload expurgé."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

from core.agent_core import AgentCore
from core.events import Event, EventBus, EventType
from core.llm_client import LLMResponse, ParsedToolCall
from core.tool_registry import ToolRegistry
from core.trace import RunTrace, read_events
from tools.base import FunctionTool, object_schema, param

RESULT_SECRET = "vault-value-42-ne-doit-pas-fuiter"
ARGUMENT_SECRET = "sk-test-1234567890"
CREDENTIAL_SECRET = "plain-credential-value"
NOTE_SECRET = "valeur-libre-sous-cle-neutre"


class _ScriptedLLM:
    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)

    def complete(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)


def _call(name: str, **arguments) -> LLMResponse:
    return LLMResponse(
        text="",
        tool_calls=[ParsedToolCall(name=name, arguments=dict(arguments), source="test")],
        finish_reason="tool_calls",
    )


def _secret_tool() -> FunctionTool:
    def read_secret(credential: str = "", passphrase: str = "", note: str = "") -> str:
        return f"{RESULT_SECRET} / {credential} / {passphrase} / {note}"

    return FunctionTool(
        "read_secret",
        "Lit un secret (outil sensible).",
        object_schema(
            {
                "credential": param("string", "Justificatif", default=""),
                "passphrase": param("string", "Phrase secrète", default=""),
                "note": param("string", "Note libre", default=""),
            },
        ),
        read_secret,
        sensitive=True,
        read_only=True,
    )


def _trace_file(directory, run_id: str):
    files = sorted(directory.glob("*.jsonl"))
    assert files, "aucune trace écrite"
    matching = [path for path in files if path.stem == run_id]
    assert matching, f"trace absente pour {run_id} : {[p.name for p in files]}"
    return matching[0]


def test_trace_writes_one_redacted_file_per_run() -> None:
    with tmpdir("trace") as tmp:
        settings = make_settings(tmp)
        bus = EventBus()
        trace = RunTrace(settings.paths.log_dir / "runs")
        unsubscribe = bus.subscribe(trace)
        core = AgentCore(
            settings,
            _ScriptedLLM(
                _call(
                    "read_secret",
                    credential=CREDENTIAL_SECRET,
                    passphrase=f"Authorization: Bearer {ARGUMENT_SECRET}",
                    note=NOTE_SECRET,
                ),
                LLMResponse(text="fait", finish_reason="stop"),
            ),
            ToolRegistry([_secret_tool()]),
            bus=bus,
        )
        try:
            reply = core.run("lis le secret")
        finally:
            unsubscribe()
            trace.close()

        assert reply.ok
        path = _trace_file(trace.directory, core.run_id)
        raw = path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in raw.splitlines()]

        assert records[0]["run_id"] == core.run_id
        assert records[0]["type"] == "run_started"
        assert records[0]["timestamp"].endswith("+00:00")
        assert all(record["run_id"] == core.run_id for record in records)
        assert {"payload", "seq", "type"} <= set(records[0])

        finished = [r for r in records if r["type"] == "tool_finished"]
        assert finished and finished[0]["payload"]["sensitive"] is True
        started = [r for r in records if r["type"] == "tool_started"]
        assert started, "l'outil sensible n'a pas été tracé"
        assert started[0]["payload"]["sensitive"] is True
        assert started[0]["payload"]["arguments"] == {}, (
            "les arguments d'un outil sensible ne doivent pas être tracés"
        )
        assert RESULT_SECRET not in raw, "un résultat d'outil sensible a fuité"
        assert ARGUMENT_SECRET not in raw, "un secret passé en argument a fuité"
        assert CREDENTIAL_SECRET not in raw, "un argument sensible a fuité"
        assert NOTE_SECRET not in raw, (
            "une valeur libre sous une clé neutre ne doit pas être tracée"
        )
        assert "credential" not in raw and "passphrase" not in raw, (
            "même le nom des arguments sensibles doit rester hors de la trace"
        )


def _old_trace(directory: Path, name: str, *, age_days: float = 0.0, size: int = 0) -> Path:
    path = directory / f"{name}.jsonl"
    body = '{"type": "run_started", "payload": {}, "seq": 0, "run_id": "x"}\n'
    path.write_text(body + "x" * size, encoding="utf-8")
    if age_days:
        stamp = time.time() - age_days * 86_400
        os.utime(path, (stamp, stamp))
    return path


def test_retention_caps_run_count_and_age() -> None:
    with tmpdir("retention") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        for index in range(5):
            _old_trace(directory, f"ancien{index}")
        too_old = _old_trace(directory, "tres-vieux", age_days=40)

        trace = RunTrace(directory, max_runs=3, max_age_days=30, max_bytes=0)
        trace(Event(type=EventType.RUN_STARTED, run_id="courant"))
        trace.close()

        names = sorted(path.stem for path in directory.glob("*.jsonl"))
        assert "courant" in names, "la trace courante ne doit jamais être supprimée"
        assert not too_old.exists(), "une trace plus vieille que la limite disparaît"
        assert len(names) == 3, f"max_runs=3 doit laisser 3 fichiers, pas {names}"


def test_retention_caps_total_size() -> None:
    with tmpdir("retention") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        for index in range(4):
            _old_trace(directory, f"lourd{index}", size=1_000)

        trace = RunTrace(directory, max_runs=50, max_age_days=0, max_bytes=2_500)
        trace(Event(type=EventType.RUN_STARTED, run_id="courant"))
        trace.close()

        paths = list(directory.glob("*.jsonl"))
        names = sorted(path.stem for path in paths)
        total = sum(path.stat().st_size for path in paths)
        assert "courant" in names, "la trace courante ne doit jamais être supprimée"
        assert total <= 2_500, f"taille totale {total} au-delà du plafond"
        assert len(names) < 5, "les plus anciennes doivent avoir été supprimées"


def test_trace_file_is_bounded_while_writing() -> None:
    """Un run bavard seul ne dépasse pas `max_bytes` : l'écriture s'arrête."""
    with tmpdir("plafond") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, max_runs=10, max_age_days=0, max_bytes=400)
        trace(Event(type=EventType.RUN_STARTED, run_id="bavard"))
        for _ in range(50):
            trace(
                Event(
                    type=EventType.STREAM_CHUNK,
                    payload={"step": 1, "text": "x" * 50},
                    run_id="bavard",
                )
            )
        trace.close()

        assert trace.path is not None
        size = trace.path.stat().st_size
        assert trace.truncated is True, "le plafond doit être signalé"
        assert size <= 400, f"fichier de {size} octets au-delà du plafond"
        records = list(read_events(trace.path))
        assert records and records[0]["type"] == "run_started"
        assert len(records) < 51, "toutes les lignes ont été écrites malgré le plafond"


def test_read_events_ignores_a_truncated_final_line() -> None:
    with tmpdir("lecture") as tmp:
        path = Path(tmp) / "run.jsonl"
        path.write_text(
            '{"type": "run_started", "seq": 0}\n'
            "\n"
            '{"type": "final", "seq": 1}\n'
            '{"type": "run_finished", "seq": 2',
            encoding="utf-8",
        )

        records = list(read_events(path))

        assert [record["type"] for record in records] == ["run_started", "final"], (
            "une dernière ligne tronquée est ignorée, pas les précédentes"
        )


def test_trace_opens_a_new_file_for_each_run() -> None:
    with tmpdir("trace") as tmp:
        settings = make_settings(tmp)
        bus = EventBus()
        trace = RunTrace(settings.paths.log_dir / "runs")
        unsubscribe = bus.subscribe(trace)
        core = AgentCore(
            settings,
            _ScriptedLLM(
                LLMResponse(text="un", finish_reason="stop"),
                LLMResponse(text="deux", finish_reason="stop"),
            ),
            ToolRegistry([]),
            bus=bus,
        )
        try:
            assert core.run("premier").ok
            first, first_id = trace.path, core.run_id
            assert core.run("second").ok
            second, second_id = trace.path, core.run_id
        finally:
            unsubscribe()
            trace.close()

        assert first != second
        assert first is not None and first.stem == first_id
        assert second is not None and second.stem == second_id
        assert first.read_text(encoding="utf-8").count("run_started") == 1
        assert second.read_text(encoding="utf-8").count("run_started") == 1
