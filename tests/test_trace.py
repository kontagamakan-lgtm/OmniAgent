"""Trace JSONL : un fichier par exécution, payload expurgé."""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

from support import make_settings, tmpdir  # noqa: I001

import config
from core.agent_core import AgentCore
from core.events import Event, EventBus, EventType
from core.llm_client import LLMResponse, ParsedToolCall
from core.tool_registry import ToolRegistry
from core.run_index import RunIndex
from core.trace import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RUNS,
    RunTrace,
    TraceFormatError,
    build_trace,
    read_events,
    read_trace,
)
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


def test_retention_spares_a_trace_opened_by_another_process() -> None:
    """Un run concurrent n'est pas « le plus ancien » : son marqueur le protège."""
    with tmpdir("actif") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        live = _old_trace(directory, "vivant", age_days=90)
        marker = directory / "vivant.active"
        marker.write_text('{"pid": 4242}', encoding="utf-8")

        trace = RunTrace(directory, max_runs=1, max_age_days=1, max_bytes=0)
        trace(Event(type=EventType.RUN_STARTED, run_id="courant"))
        trace.close()

        assert live.exists(), "une trace ouverte ailleurs ne doit pas être purgée"
        assert marker.exists(), "le marqueur d'un autre processus ne doit pas être effacé"


def test_a_stale_activity_marker_stops_protecting_its_trace() -> None:
    """Processus tué : le marqueur périmé ne bloque plus la rétention."""
    with tmpdir("perime") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        dead = _old_trace(directory, "mort", age_days=90)
        marker = directory / "mort.active"
        marker.write_text('{"pid": 4243}', encoding="utf-8")
        old = time.time() - 3_600
        os.utime(marker, (old, old))

        trace = RunTrace(directory, max_runs=1, max_age_days=1, max_bytes=0, active_ttl_s=60)
        trace(Event(type=EventType.RUN_STARTED, run_id="courant"))
        trace.close()

        assert not marker.exists(), "le marqueur périmé doit être rangé"
        assert not dead.exists(), "la trace n'est plus protégée : elle est purgée"


def test_trace_closes_its_activity_marker() -> None:
    with tmpdir("marqueur") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory)
        trace(Event(type=EventType.RUN_STARTED, run_id="seul"))
        assert (directory / "seul.active").exists(), "le marqueur doit exister pendant le run"
        trace(Event(type=EventType.RUN_FINISHED, payload={}, run_id="seul"))
        assert not (directory / "seul.active").exists(), "le marqueur doit disparaître à la fin"


def test_the_ceiling_leaves_a_visible_truncation_record() -> None:
    """Une trace coupée doit le dire elle-même, pas seulement en mémoire."""
    with tmpdir("coupe") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, max_runs=5, max_age_days=0, max_bytes=4_000)
        trace(Event(type=EventType.RUN_STARTED, run_id="bavard"))
        for _ in range(60):
            trace(
                Event(
                    type=EventType.STREAM_CHUNK,
                    payload={"step": 1, "text": "z" * 120},
                    run_id="bavard",
                )
            )
        trace.close()

        assert trace.truncated is True
        assert trace.path is not None
        assert trace.path.stat().st_size <= 4_000
        records = list(read_events(trace.path))
        assert records[-1]["type"] == EventType.TRACE_TRUNCATED, (
            "le dernier enregistrement doit annoncer la coupure"
        )
        assert records[-1]["payload"]["max_bytes"] == 4_000
        assert trace.summary()["truncated"] is True


def test_metadata_trace_keeps_facts_without_session_content() -> None:
    with tmpdir("meta") as tmp:
        settings = make_settings(tmp)
        bus = EventBus()
        trace = RunTrace(settings.paths.log_dir / "runs", content="metadata")
        unsubscribe = bus.subscribe(trace)
        core = AgentCore(
            settings,
            _ScriptedLLM(LLMResponse(text="réponse confidentielle", finish_reason="stop")),
            ToolRegistry([]),
            bus=bus,
        )
        try:
            assert core.run("tâche confidentielle").ok
        finally:
            unsubscribe()
            trace.close()

        raw = _trace_file(trace.directory, core.run_id).read_text(encoding="utf-8")
        assert "confidentielle" not in raw, "le contenu de la session ne doit pas rester"
        types = [json.loads(line)["type"] for line in raw.splitlines()]
        assert "run_started" in types and "final" in types
        assert "stream_chunk" not in types, "aucun fragment de réponse en mode métadonnées"
        started = json.loads(raw.splitlines()[0])
        assert started["payload"]["model"], "les métadonnées du manifeste restent"
        assert "task" not in started["payload"]


def test_archive_compresses_instead_of_deleting() -> None:
    with tmpdir("archive") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        for index in range(3):
            _old_trace(directory, f"vieux{index}")

        trace = RunTrace(
            directory, max_runs=2, max_age_days=0, max_bytes=0, archive=True
        )
        trace(Event(type=EventType.RUN_STARTED, run_id="courant"))
        trace.close()

        archives = sorted(directory.glob("*.jsonl.gz"))
        assert archives, "les vieilles traces doivent être archivées, pas détruites"
        with gzip.open(archives[0], "rt", encoding="utf-8") as handle:
            assert "run_started" in handle.read()
        assert len(list(directory.glob("*.jsonl"))) == 2, (
            "max_runs compte les traces en clair, pas les archives"
        )


def test_reader_report_distinguishes_corruption_from_a_cut() -> None:
    with tmpdir("bilan") as tmp:
        path = Path(tmp) / "run.jsonl"
        path.write_text(
            '{"type": "run_started", "seq": 0}\n'
            "\n"
            '{"type": "final", "seq": 1}\n'
            '{"type": "run_finished", "seq": 2}\n',
            encoding="utf-8",
        )

        records, report = read_trace(path)

        assert [record["type"] for record in records] == ["run_started", "final", "run_finished"]
        assert report.ignored == 1 and report.events == 3 and report.lines == 4
        assert report.truncated_final is False, (
            "une ligne vide au milieu n'est pas une coupure finale"
        )
        try:
            list(read_events(path, strict=True))
        except TraceFormatError:
            pass
        else:
            raise AssertionError("le mode strict doit signaler la ligne illisible")


def test_grouped_fsync_keeps_every_event() -> None:
    with tmpdir("fsync") as tmp:
        trace = RunTrace(
            Path(tmp) / "runs", max_age_days=0, max_bytes=0, fsync_every=2
        )
        trace(Event(type=EventType.RUN_STARTED, run_id="r"))
        for index in range(5):
            trace(Event(type=EventType.STEP_STARTED, payload={"index": index}, run_id="r"))
        trace(Event(type=EventType.RUN_FINISHED, payload={}, run_id="r"))

        records = list(read_events(trace.path))
        assert len(records) == 7
        assert records[-1]["type"] == EventType.RUN_FINISHED


def test_run_started_carries_a_manifest() -> None:
    with tmpdir("manifeste") as tmp:
        settings = make_settings(tmp)
        bus = EventBus()
        trace = RunTrace(settings.paths.log_dir / "runs")
        unsubscribe = bus.subscribe(trace)
        core = AgentCore(
            settings,
            _ScriptedLLM(LLMResponse(text="ok", finish_reason="stop")),
            ToolRegistry([]),
            bus=bus,
        )
        try:
            assert core.run("manifeste").ok
        finally:
            unsubscribe()
            trace.close()

        payload = json.loads(_trace_file(trace.directory, core.run_id).read_text(encoding="utf-8").splitlines()[0])["payload"]
        assert payload["app_version"] == config.APP_VERSION
        assert payload["trace_format"] == config.TRACE_FORMAT_VERSION >= 2
        assert payload["model"] == settings.llm.model
        assert payload["backend"] == settings.llm.backend
        assert payload["budget_tokens"] == settings.agent.max_context_tokens
        digest = payload["settings_digest"]
        assert len(digest) == 16 and int(digest, 16) >= 0, "empreinte hexadécimale attendue"


def test_trace_settings_drive_defaults_and_validation() -> None:
    settings = config.Settings()
    assert settings.trace.enabled is True and settings.trace.content == "full"
    assert settings.trace.max_runs == DEFAULT_MAX_RUNS
    assert settings.trace.max_age_days == DEFAULT_MAX_AGE_DAYS
    assert settings.trace.max_bytes == DEFAULT_MAX_BYTES

    settings.trace.max_runs = 0
    settings.trace.content = "tout"
    settings.trace.fsync_every = -1
    settings.trace.max_bytes = -1
    errors = {issue.field for issue in settings.validate() if issue.level == "error"}
    assert {"trace.max_runs", "trace.content", "trace.fsync_every", "trace.max_bytes"} <= errors


def test_build_trace_honours_the_configuration() -> None:
    with tmpdir("reglage") as tmp:
        settings = make_settings(tmp)
        settings.trace.enabled = False
        assert build_trace(settings) is None, "trace désactivée : rien ne doit être créé"

        settings.trace.enabled = True
        settings.trace.content = "metadata"
        settings.trace.max_runs = 7
        settings.trace.archive = True
        trace = build_trace(settings)
        assert trace is not None
        assert trace.directory == settings.paths.log_dir / "runs"
        assert trace.content == "metadata"
        assert trace.max_runs == 7 and trace.archive is True


def test_build_trace_can_group_runs_by_workspace() -> None:
    with tmpdir("espaces") as tmp:
        settings = make_settings(tmp)
        settings.trace.per_workspace = True

        trace = build_trace(settings)

        assert trace is not None
        assert trace.directory.parent == settings.paths.log_dir / "runs"
        assert trace.directory != settings.paths.log_dir / "runs"
        assert trace.directory.name


def test_a_finished_run_is_archived_at_close() -> None:
    with tmpdir("archive-fin") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, archive=True, archive_min_bytes=1)
        trace(Event(type=EventType.RUN_STARTED, run_id="fin", seq=1))
        trace(Event(type=EventType.RUN_FINISHED, run_id="fin", payload={}, seq=2))

        assert trace.path is not None and trace.path.name.endswith(".gz")
        assert not (directory / "fin.jsonl").exists()
        records = list(read_events(trace.path))
        assert records[-1]["type"] == EventType.RUN_FINISHED

        index = RunIndex(directory)
        assert len(index.path.read_text(encoding="utf-8").splitlines()) == 1, (
            "l'archivage à la fermeture ne doit pas laisser deux lignes"
        )
        assert index.entries()["fin"].path == str(trace.path)


def test_run_index_records_started_and_finished_states() -> None:
    with tmpdir("index") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory)
        trace(
            Event(
                type=EventType.RUN_STARTED,
                run_id="idx",
                payload={"app_version": "9.9", "model": "m", "trace_format": 2},
                seq=1,
            )
        )
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="idx",
                payload={"stop_reason": "final", "ok": True},
                seq=2,
            )
        )

        entries = RunIndex(directory).entries()

        assert entries["idx"].state == "finished"
        assert entries["idx"].model == "m"
        assert entries["idx"].ok is True


def test_the_index_is_compacted_to_the_traces_still_present() -> None:
    with tmpdir("index-compact") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        index = RunIndex(directory)
        index.mark("garde", "started", manifest={"model": "m", "app_version": "1.0"})
        index.mark("garde", "finished", ok=True, stop_reason="final")
        index.mark("purgé", "started", manifest={"model": "m"})
        index.mark("purgé", "finished", ok=True)
        index.mark("vieux", "started")
        (directory / "garde.jsonl").write_text("", encoding="utf-8")
        (directory / "vieux.jsonl.gz").write_bytes(b"")
        before = len(index.path.read_text(encoding="utf-8").splitlines())

        assert index.compact() is True

        entries = index.entries()
        assert set(entries) == {"garde", "vieux"}, "seules les traces présentes restent"
        assert entries["garde"].state == "finished"
        assert entries["garde"].model == "m", "le manifeste du début doit survivre"
        assert entries["garde"].ok is True
        lines = index.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert before > len(lines)
        assert index.compact() is False, "un index déjà compact ne doit pas être réécrit"


def test_compaction_merges_repeated_states_of_a_present_run() -> None:
    with tmpdir("index-doublons") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        index = RunIndex(directory)
        index.mark("repris", "started", manifest={"model": "m", "app_version": "1.0"})
        index.mark("repris", "finished", ok=True, stop_reason="final")
        index.mark("repris", "started")
        index.mark("repris", "finished", ok=True, stop_reason="final")
        (directory / "repris.jsonl").write_text("", encoding="utf-8")
        before = len(index.path.read_text(encoding="utf-8").splitlines())
        assert before == 4

        assert index.compact() is True, "les états répétés doivent être dédupliqués"

        lines = index.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"index non dédupliqué : {len(lines)} lignes"
        entries = index.entries()
        assert set(entries) == {"repris"}
        assert entries["repris"].state == "finished"
        assert entries["repris"].model == "m", "le manifeste du début doit survivre"
        assert entries["repris"].ok is True
        assert index.compact() is False, "un index déjà dédupliqué ne se réécrit pas"


def test_compaction_removes_unreadable_index_lines() -> None:
    with tmpdir("index-corrompu") as tmp:
        directory = Path(tmp) / "runs"
        directory.mkdir()
        index = RunIndex(directory)
        index.mark("garde", "started", manifest={"model": "m"})
        index.mark("garde", "finished", ok=True)
        (directory / "garde.jsonl").write_text("", encoding="utf-8")
        with index.path.open("a", encoding="utf-8") as handle:
            handle.write('{"run_id": "coupé", "state": "sta' + "\n")
        assert len(index.path.read_text(encoding="utf-8").splitlines()) == 3

        assert index.compact() is True, "une ligne illisible doit déclencher la réécriture"

        lines = index.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"ligne corrompue non supprimée : {lines}"
        entry = index.entries()["garde"]
        assert entry.state == "finished" and entry.model == "m"
        assert index.compact() is False, "index propre : plus rien à réécrire"


def test_retention_keeps_the_index_bounded_to_present_traces() -> None:
    with tmpdir("index-retention") as tmp:
        directory = Path(tmp) / "runs"
        for number in range(1, 5):
            trace = RunTrace(directory, max_runs=1)
            trace(
                Event(
                    type=EventType.RUN_STARTED,
                    run_id=f"r{number}",
                    payload={"trace_format": 2},
                    seq=1,
                )
            )
            trace(
                Event(
                    type=EventType.RUN_FINISHED,
                    run_id=f"r{number}",
                    payload={"ok": True},
                    seq=2,
                )
            )

        index = RunIndex(directory)
        entries = index.entries()
        present = {
            path.name[: -len(".jsonl")] for path in directory.glob("*.jsonl")
        }

        assert present == {"r4"}, "la rétention ne garde qu'une trace"
        assert set(entries) == present, "l'index ne garde pas les runs purgés"
        assert len(index.path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_finished_run_leaves_a_single_line_in_the_index() -> None:
    with tmpdir("index-fin") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory)
        trace(
            Event(
                type=EventType.RUN_STARTED,
                run_id="seul",
                payload={"model": "m", "trace_format": 3},
                seq=1,
            )
        )
        trace(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="seul",
                payload={"ok": True, "stop_reason": "final"},
                seq=2,
            )
        )

        index = RunIndex(directory)
        lines = index.path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == 1, f"index non compacté après la fin : {len(lines)} lignes"
        entry = index.entries()["seul"]
        assert entry.state == "finished" and entry.model == "m"
        assert trace.path is not None and entry.bytes == trace.path.stat().st_size


def test_rebuild_infers_the_finished_state_from_the_file_end() -> None:
    with tmpdir("index-rebuild-etats") as tmp:
        directory = Path(tmp) / "runs"
        plain = RunTrace(directory)
        plain(
            Event(
                type=EventType.RUN_STARTED,
                run_id="fini",
                payload={"model": "m", "trace_format": 3},
                seq=1,
            )
        )
        plain(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="fini",
                payload={"ok": True, "stop_reason": "final"},
                seq=2,
            )
        )
        archived = RunTrace(directory, archive=True, archive_min_bytes=1)
        archived(
            Event(
                type=EventType.RUN_STARTED,
                run_id="archive",
                payload={"model": "m", "trace_format": 3},
                seq=1,
            )
        )
        archived(
            Event(
                type=EventType.RUN_FINISHED,
                run_id="archive",
                payload={"ok": True, "stop_reason": "final"},
                seq=2,
            )
        )
        original = RunIndex(directory).entries()
        assert {run_id: entry.state for run_id, entry in original.items()} == {
            "fini": "finished",
            "archive": "finished",
        }

        rebuilt = RunIndex(directory).rebuild(reset=True)

        assert {run_id: entry.state for run_id, entry in rebuilt.items()} == {
            run_id: entry.state for run_id, entry in original.items()
        }, "la reconstruction doit retrouver l'état d'origine"
        assert rebuilt["archive"].path.endswith(".jsonl.gz")
        assert rebuilt["fini"].model == "m"


def test_a_killed_run_is_marked_interrupted_by_the_next_one() -> None:
    with tmpdir("interrompu") as tmp:
        directory = Path(tmp) / "runs"
        dead = RunTrace(directory)
        dead(
            Event(
                type=EventType.RUN_STARTED,
                run_id="mort",
                payload={"task": "x", "trace_format": 2},
                seq=1,
            )
        )
        dead.close()  # marqueur retiré, aucun run_finished : processus tué

        alive = RunTrace(directory)
        alive(Event(type=EventType.RUN_STARTED, run_id="vivant", payload={"trace_format": 2}, seq=1))
        alive(Event(type=EventType.RUN_FINISHED, run_id="vivant", payload={}, seq=2))

        entries = RunIndex(directory).entries()

        assert entries["mort"].state == "interrupted"
        assert entries["vivant"].state == "finished"


def test_heartbeat_refreshes_the_activity_marker_without_events() -> None:
    """Un run silencieux (outil long) ne doit pas paraître abandonné."""
    with tmpdir("battement") as tmp:
        directory = Path(tmp) / "runs"
        trace = RunTrace(directory, active_ttl_s=1.0)
        trace(Event(type=EventType.RUN_STARTED, run_id="battant", seq=1))
        marker = directory / "battant.active"
        old = time.time() - 10
        os.utime(marker, (old, old))

        deadline = time.time() + 5
        while time.time() < deadline and marker.stat().st_mtime <= old + 1:
            time.sleep(0.05)

        assert marker.stat().st_mtime > old + 1, "le marqueur n'a pas été rafraîchi"
        trace.close()
