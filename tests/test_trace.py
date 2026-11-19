"""Trace JSONL : un fichier par exécution, payload expurgé."""

from __future__ import annotations

import json

from support import make_settings, tmpdir  # noqa: I001

from core.agent_core import AgentCore
from core.events import EventBus
from core.llm_client import LLMResponse, ParsedToolCall
from core.tool_registry import ToolRegistry
from core.trace import RunTrace
from tools.base import FunctionTool, object_schema, param

RESULT_SECRET = "vault-value-42-ne-doit-pas-fuiter"
ARGUMENT_SECRET = "sk-test-1234567890"
CREDENTIAL_SECRET = "plain-credential-value"


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
    def read_secret(credential: str = "", passphrase: str = "") -> str:
        return f"{RESULT_SECRET} / {credential} / {passphrase}"

    return FunctionTool(
        "read_secret",
        "Lit un secret (outil sensible).",
        object_schema(
            {
                "credential": param("string", "Justificatif", default=""),
                "passphrase": param("string", "Phrase secrète", default=""),
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
        assert RESULT_SECRET not in raw, "un résultat d'outil sensible a fuité"
        assert ARGUMENT_SECRET not in raw, "un secret passé en argument a fuité"
        assert CREDENTIAL_SECRET not in raw, (
            "un argument nommé credential doit être masqué par son nom"
        )
        assert "MASQUÉ" in raw, "le masquage doit rester visible dans la trace"


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
