"""Rejeu hors-ligne d'une trace JSONL.

Une trace sert d'abord à comprendre après coup : ce module la relit sans agent,
sans réseau et sans interface, et en reconstitue le déroulé (manifeste, étapes,
appels d'outils, réponse finale). C'est aussi ce qui rend le format vérifiable :
si le rejeu ne retrouve pas les faits, la trace a un problème.

    python -m core.replay data/logs/runs/<run>.jsonl
    python -m core.replay <trace> --json
    python -m core.replay <trace> --check          # vérifie le format
    python -m core.replay <trace> --diff autre.jsonl
    python -m core.replay <trace> --export rapport.json
    python -m core.replay --index-compact [dossier]
    python -m core.replay --index-rebuild [dossier]

`TraceReplayLLM` réinjecte les réponses du modèle lues dans une trace : la
boucle ReAct peut alors être exercée hors-ligne, sur un scénario réel, sans
fournisseur ni doublure écrite à la main. Les arguments des outils sensibles ne
sont pas dans la trace (c'est voulu) : un rejeu de ces appels part avec des
arguments vides et le dit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from config import TRACE_FORMAT_VERSION
from core.events import EventType
from core.llm_client import LLMResponse
from core.react_parser import ParsedToolCall
from core.run_index import maintain
from core.trace import TraceReadReport, read_trace
from core.trace_crypto import KEY_ENV, TraceKeyError, optional_key

__all__ = [
    "ReplayExhausted",
    "ReplayRun",
    "ReplayStep",
    "TraceReplayLLM",
    "export_bundle",
    "format_run",
    "main",
    "manifest_diff",
    "replay",
]

#: Champs du manifeste comparés par `manifest_diff` (aucun n'est secret).
MANIFEST_FIELDS = (
    "app_version",
    "trace_format",
    "model",
    "backend",
    "budget_tokens",
    "settings_digest",
    "workspace",
)

#: Champs du bilan de run comparés par `manifest_diff`.
SUMMARY_FIELDS = (
    "duration_s",
    "steps",
    "tool_calls",
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "trims",
    "trim_cache_hits",
)


class ReplayExhausted(RuntimeError):
    """La trace ne contient plus de réponse à réinjecter."""


@dataclass(slots=True)
class ReplayStep:
    """Une étape relue : texte éventuel et appels d'outils observés."""

    index: int
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReplayRun:
    """Déroulé complet reconstitué depuis une trace."""

    path: Path
    run_id: str = ""
    task: str = ""
    app_version: str = ""
    model: str = ""
    backend: str = ""
    settings_digest: str = ""
    workspace: str = ""
    budget_tokens: int = 0
    trace_format: int = 0
    steps: list[ReplayStep] = field(default_factory=list)
    final: str = ""
    stop_reason: str = ""
    ok: bool = False
    truncated: bool = False
    interrupted: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    report: TraceReadReport = field(default_factory=TraceReadReport)

    @property
    def tools(self) -> list[str]:
        return [call["tool"] for step in self.steps for call in step.calls]

    def manifest(self) -> dict[str, Any]:
        """Vue comparable : de quoi dire en quoi deux runs diffèrent."""
        return {
            "app_version": self.app_version,
            "trace_format": self.trace_format,
            "model": self.model,
            "backend": self.backend,
            "budget_tokens": self.budget_tokens,
            "settings_digest": self.settings_digest,
            "workspace": self.workspace,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "run_id": self.run_id,
            "task": self.task,
            "app_version": self.app_version,
            "model": self.model,
            "backend": self.backend,
            "settings_digest": self.settings_digest,
            "workspace": self.workspace,
            "budget_tokens": self.budget_tokens,
            "trace_format": self.trace_format,
            "final": self.final,
            "stop_reason": self.stop_reason,
            "ok": self.ok,
            "truncated": self.truncated,
            "interrupted": self.interrupted,
            "summary": dict(self.summary),
            "steps": [
                {"index": step.index, "text": step.text, "calls": list(step.calls)}
                for step in self.steps
            ],
            "errors": list(self.errors),
            "report": self.report.as_dict(),
        }


def _step(steps: list[ReplayStep], index: int) -> ReplayStep:
    for step in steps:
        if step.index == index:
            return step
    created = ReplayStep(index=index)
    steps.append(created)
    return created


def replay(
    path: Path | str, *, strict: bool = False, key: bytes | None = None
) -> ReplayRun:
    """Reconstitue une exécution depuis son fichier de trace.

    Le manifeste de `run_started` fournit le contexte (version, modèle,
    condensat des réglages) ; sans lui, un rejeu ne saurait pas dans quelles
    conditions la trace a été produite. Une trace chiffrée exige `key`, sinon
    `TraceKeyError` plutôt qu'un rejeu vide.
    """
    target = Path(path)
    records, report = read_trace(target, strict=strict, key=key)
    run = ReplayRun(path=target, report=report)
    steps: list[ReplayStep] = []
    finished = False

    for record in records:
        event_type = str(record.get("type") or "")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if not run.run_id and record.get("run_id"):
            run.run_id = str(record["run_id"])

        if event_type == EventType.RUN_STARTED:
            run.task = str(payload.get("task") or "")
            run.app_version = str(payload.get("app_version") or "")
            run.model = str(payload.get("model") or "")
            run.backend = str(payload.get("backend") or "")
            run.settings_digest = str(payload.get("settings_digest") or "")
            run.workspace = str(payload.get("workspace") or "")
            run.budget_tokens = int(payload.get("budget_tokens") or 0)
            run.trace_format = int(payload.get("trace_format") or 0)
        elif event_type == EventType.STEP_STARTED:
            _step(steps, int(payload.get("index") or 0))
        elif event_type == EventType.TOOL_STARTED:
            step = _step(steps, int(payload.get("step") or 0))
            step.calls.append(
                {
                    "tool": str(payload.get("tool") or ""),
                    "sensitive": bool(payload.get("sensitive")),
                    "arguments": payload.get("arguments") or {},
                    "ok": True,
                    "error_code": "",
                    "duration_ms": 0.0,
                }
            )
        elif event_type == EventType.TOOL_FINISHED:
            step = _step(steps, int(payload.get("step") or 0))
            name = str(payload.get("tool") or "")
            for call in reversed(step.calls):
                if call["tool"] == name:
                    call["ok"] = bool(payload.get("ok", True))
                    call["error_code"] = str(payload.get("error_code") or "")
                    call["duration_ms"] = float(payload.get("duration_ms") or 0.0)
                    break
        elif event_type == EventType.LLM_RESPONSE:
            text = str(payload.get("text") or "")
            if text:
                _step(steps, int(payload.get("step") or 0)).text = text
        elif event_type == EventType.FINAL:
            index = int(payload.get("step") or 0)
            if not run.final:
                run.final = str(payload.get("text") or "")
            _step(steps, index)
        elif event_type == EventType.ERROR:
            run.errors.append(str(payload.get("message") or payload.get("code") or "erreur"))
        elif event_type == EventType.RUN_FINISHED:
            finished = True
            run.stop_reason = str(payload.get("stop_reason") or "")
            run.ok = bool(payload.get("ok"))
            summary = payload.get("summary")
            if isinstance(summary, dict):
                run.summary = dict(summary)
        elif event_type == EventType.TRACE_TRUNCATED:
            run.truncated = True

    run.steps = sorted(steps, key=lambda item: item.index)
    # Sans fin ni troncature, le fichier vient d'un processus tué : c'est un
    # fait à dire, pas un détail de lecture.
    run.interrupted = not finished and not run.truncated
    if run.trace_format and run.trace_format > TRACE_FORMAT_VERSION:
        run.errors.append(
            f"trace de format {run.trace_format} > {TRACE_FORMAT_VERSION} : rejeu partiel"
        )
    return run


def manifest_diff(left: ReplayRun, right: ReplayRun) -> list[str]:
    """Différences de manifeste et de bilan entre deux runs, en clair."""
    lines: list[str] = []
    left_manifest, right_manifest = left.manifest(), right.manifest()
    for name in MANIFEST_FIELDS:
        before, after = left_manifest.get(name), right_manifest.get(name)
        if before != after:
            lines.append(f"{name} : {before!r} -> {after!r}")
    for name in SUMMARY_FIELDS:
        before = left.summary.get(name)
        after = right.summary.get(name)
        if before is None and after is None:
            continue
        if before != after:
            lines.append(f"bilan.{name} : {before!r} -> {after!r}")
    return lines


def format_run(run: ReplayRun) -> str:
    """Rendu lisible d'un rejeu, sans caractère hors cp1252."""
    lines: list[str] = [f"Trace : {run.path.name} (run {run.run_id or '?'})"]
    header = f"  version {run.app_version or '?'}"
    if run.model:
        header += f", modele {run.model}"
    if run.backend:
        header += f" ({run.backend})"
    if run.settings_digest:
        header += f", reglages {run.settings_digest}"
    if run.budget_tokens:
        header += f", budget {run.budget_tokens} tokens"
    lines.append(header)
    if run.workspace:
        lines.append(f"  espace : {run.workspace}")
    if run.task:
        lines.append(f"  tache : {run.task}")
    lines.append("")
    for step in run.steps:
        if step.text:
            first = " ".join(step.text.split())[:160]
            lines.append(f"Etape {step.index} : {first}")
        else:
            lines.append(f"Etape {step.index}")
        for call in step.calls:
            status = "ok" if call["ok"] else f"echec {call['error_code'] or '?'}"
            flags = " [sensible]" if call["sensitive"] else ""
            lines.append(f"  . {call['tool']} ({status}){flags}")
    if run.final:
        lines.append("")
        lines.append("Reponse finale :")
        lines.append(run.final)
    lines.append("")
    outcome = run.stop_reason or "?"
    lines.append(f"Arret : {outcome} (ok={run.ok})")
    if run.summary:
        pairs = [
            f"{name}={run.summary[name]}"
            for name in SUMMARY_FIELDS
            if name in run.summary
        ]
        lines.append("Bilan : " + ", ".join(pairs))
    if run.truncated:
        lines.append("Trace TRONQUEE : la fin de l'execution n'a pas ete enregistree.")
    if run.interrupted:
        lines.append("Run INTERROMPU : aucun run_finished, processus probablement tue.")
    ignored = run.report.ignored
    lines.append(
        f"{run.report.events} evenement(s) lus, {ignored} ligne(s) ignoree(s), "
        f"{len(run.steps)} etape(s), {len(run.tools)} appel(s) d'outil"
    )
    if ignored and not run.report.truncated_final:
        lines.append("Attention : des lignes illisibles ne sont pas en fin de fichier.")
    for error in run.errors:
        lines.append(f"Probleme : {error}")
    return "\n".join(lines)


def export_bundle(run: ReplayRun) -> dict[str, Any]:
    """Version partageable d'un run : faits seulement, aucun contenu de session.

    Liste blanche stricte : ni tâche, ni réponse, ni arguments d'outils. Le
    fichier peut être joint à un rapport de bug sans exposer le travail de
    l'utilisateur.
    """
    return {
        "export_format": 1,
        "run_id": run.run_id,
        "app_version": run.app_version,
        "trace_format": run.trace_format,
        "model": run.model,
        "backend": run.backend,
        "settings_digest": run.settings_digest,
        "workspace": run.workspace,
        "budget_tokens": run.budget_tokens,
        "stop_reason": run.stop_reason,
        "ok": run.ok,
        "truncated": run.truncated,
        "interrupted": run.interrupted,
        "summary": dict(run.summary),
        "errors": list(run.errors[-10:]),
        "report": run.report.as_dict(),
        "steps": [
            {
                "index": step.index,
                "calls": [
                    {
                        "tool": call["tool"],
                        "ok": call["ok"],
                        "error_code": call["error_code"],
                        "sensitive": call["sensitive"],
                        "duration_ms": call["duration_ms"],
                    }
                    for call in step.calls
                ],
            }
            for step in run.steps
        ],
    }


class TraceReplayLLM:
    """Fournisseur qui rejoue les réponses d'une trace, pour rejouer la boucle.

    Le script est reconstitué à partir de `llm_response` (texte, raison d'arrêt)
    et de `tool_started` (nom et arguments réellement exécutés). Les arguments
    d'un outil sensible ne sont pas dans la trace : ils reviennent vides, ce que
    la trace dit explicitement.
    """

    supports_stream = False

    def __init__(self, path: Path | str, *, key: bytes | None = None) -> None:
        self.path = Path(path)
        self._script = _llm_script(self.path, key=key)
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._script)

    @property
    def remaining(self) -> int:
        return max(0, len(self._script) - self._cursor)

    def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        if self._cursor >= len(self._script):
            raise ReplayExhausted(
                f"la trace {self.path.name} ne contient plus de réponse à rejouer"
            )
        entry = self._script[self._cursor]
        self._cursor += 1
        calls = [
            ParsedToolCall(
                name=str(call["name"]),
                arguments=dict(call.get("arguments") or {}),
                source="trace",
            )
            for call in entry["calls"]
        ]
        finish = str(entry.get("finish") or "")
        if not finish:
            finish = "tool_calls" if calls else "stop"
        return LLMResponse(text=str(entry.get("text") or ""), tool_calls=calls, finish_reason=finish)

    def close(self) -> None:  # compatibilité avec l'interface LLMClient
        return


def _llm_script(path: Path, *, key: bytes | None = None) -> list[dict[str, Any]]:
    """Réponses du modèle, dans l'ordre, avec les appels qui les ont suivies."""
    try:
        records, _report = read_trace(path, key=key)
    except OSError:
        return []
    steps: dict[int, dict[str, Any]] = {}
    for record in records:
        event_type = str(record.get("type") or "")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        index = int(payload.get("step") or 0)
        if event_type == EventType.LLM_RESPONSE:
            entry = steps.setdefault(
                index, {"text": "", "calls": [], "finish": ""}
            )
            entry["text"] = str(payload.get("text") or entry["text"])
            entry["finish"] = str(payload.get("finish_reason") or entry["finish"])
        elif event_type == EventType.TOOL_STARTED:
            entry = steps.setdefault(
                index, {"text": "", "calls": [], "finish": "tool_calls"}
            )
            entry["calls"].append(
                {
                    "name": str(payload.get("tool") or ""),
                    "arguments": payload.get("arguments") or {},
                }
            )
            entry["finish"] = "tool_calls"
        elif event_type == EventType.FINAL:
            entry = steps.setdefault(index, {"text": "", "calls": [], "finish": "stop"})
            entry["text"] = str(payload.get("text") or entry["text"])
            entry["finish"] = "stop"
    return [steps[index] for index in sorted(steps)]


def _default_runs_dir() -> Path:
    """Dossier de traces de la configuration courante."""
    from config import Settings

    return Settings.load(strict=False).paths.log_dir / "runs"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.replay",
        description="Rejoue une trace d'execution sans agent ni reseau.",
    )
    parser.add_argument(
        "trace",
        type=Path,
        nargs="?",
        help="fichier .jsonl ou .jsonl.gz",
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="echoue sur une ligne vide ou illisible au lieu de l'ignorer",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verifie les invariants du format au lieu de rejouer",
    )
    parser.add_argument(
        "--diff",
        metavar="AUTRE",
        type=Path,
        help="compare le manifeste et le bilan avec une autre trace",
    )
    parser.add_argument(
        "--export",
        metavar="FICHIER",
        type=Path,
        help="ecrit un bundle partageable (sans tache ni reponse)",
    )
    parser.add_argument(
        "--key-env",
        default=KEY_ENV,
        metavar="NOM",
        help=f"variable contenant la cle des traces chiffrees (defaut : {KEY_ENV})",
    )
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument(
        "--index-compact",
        nargs="?",
        const="",
        metavar="DOSSIER",
        help="compacte runs.index (dossier par defaut : celui des traces)",
    )
    maintenance.add_argument(
        "--index-rebuild",
        nargs="?",
        const="",
        metavar="DOSSIER",
        help="reconstruit runs.index depuis les traces presentes",
    )
    args = parser.parse_args(argv)

    if args.index_compact is not None or args.index_rebuild is not None:
        rebuild = args.index_rebuild is not None
        chosen = args.index_rebuild if rebuild else args.index_compact
        directory = Path(chosen) if chosen else _default_runs_dir()
        if not directory.is_dir():
            print(f"Dossier de traces introuvable : {directory}", file=sys.stderr)
            return 2
        for item in maintain(directory, rebuild=rebuild):
            print(
                f"{str(item['action']).ljust(9)} {item['runs']} run(s)  {item['directory']}"
            )
        return 0

    path = args.trace
    if path is None:
        print(
            "Trace manquante : indiquez un fichier, ou --index-compact/--index-rebuild",
            file=sys.stderr,
        )
        return 2
    if not path.is_file():
        print(f"Trace introuvable : {path}", file=sys.stderr)
        return 2

    try:
        key = optional_key(args.key_env)
    except TraceKeyError as exc:
        print(f"Cle de trace illisible : {exc}", file=sys.stderr)
        return 2

    if args.check:
        from core.trace_lint import format_issues, lint_file

        issues, report = lint_file(path, strict=args.strict, key=key)
        if args.json:
            print(
                json.dumps(
                    {
                        "trace": str(path),
                        "report": report.as_dict(),
                        "issues": [issue.as_dict() for issue in issues],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Trace : {path}")
            print(format_issues(issues, report))
        return 1 if any(issue.error for issue in issues) else 0

    try:
        run = replay(path, strict=args.strict, key=key)
    except Exception as exc:  # noqa: BLE001 - la CLI rapporte, elle ne plante pas
        print(f"Trace illisible : {exc}", file=sys.stderr)
        return 2

    if args.diff is not None:
        if not args.diff.is_file():
            print(f"Trace introuvable : {args.diff}", file=sys.stderr)
            return 2
        other = replay(args.diff, strict=args.strict, key=key)
        differences = manifest_diff(run, other)
        print(f"A : {path.name} ({run.run_id or '?'})")
        print(f"B : {args.diff.name} ({other.run_id or '?'})")
        if differences:
            print("\n".join(differences))
        else:
            print("aucune difference de manifeste ni de bilan")
        return 0

    if args.export is not None:
        bundle = export_bundle(run)
        try:
            args.export.write_text(
                json.dumps(bundle, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Ecriture impossible : {exc}", file=sys.stderr)
            return 2
        print(f"Bundle ecrit : {args.export}")
        print(
            "Contenu : manifeste, etapes, outils, bilan. Ni tache, ni reponse, "
            "ni arguments d'outils."
        )
        return 0

    if args.json:
        print(json.dumps(run.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(format_run(run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
