"""Rejeu hors-ligne d'une trace JSONL.

Une trace sert d'abord à comprendre après coup : ce module la relit sans agent,
sans réseau et sans interface, et en reconstitue le déroulé (manifeste, étapes,
appels d'outils, réponse finale). C'est aussi ce qui rend le format vérifiable :
si le rejeu ne retrouve pas les faits, la trace a un problème.

    python -m core.replay data/logs/runs/<run>.jsonl
    python -m core.replay <trace> --json
    python -m core.replay <trace> --strict     # echoue sur une ligne illisible
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
from core.trace import TraceReadReport, read_trace

__all__ = ["ReplayRun", "ReplayStep", "format_run", "main", "replay"]


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
    budget_tokens: int = 0
    trace_format: int = 0
    steps: list[ReplayStep] = field(default_factory=list)
    final: str = ""
    stop_reason: str = ""
    ok: bool = False
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    report: TraceReadReport = field(default_factory=TraceReadReport)

    @property
    def tools(self) -> list[str]:
        return [call["tool"] for step in self.steps for call in step.calls]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "run_id": self.run_id,
            "task": self.task,
            "app_version": self.app_version,
            "model": self.model,
            "backend": self.backend,
            "settings_digest": self.settings_digest,
            "budget_tokens": self.budget_tokens,
            "trace_format": self.trace_format,
            "final": self.final,
            "stop_reason": self.stop_reason,
            "ok": self.ok,
            "truncated": self.truncated,
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


def replay(path: Path | str, *, strict: bool = False) -> ReplayRun:
    """Reconstitue une exécution depuis son fichier de trace.

    Le manifeste de `run_started` fournit le contexte (version, modèle,
    condensat des réglages) ; sans lui, un rejeu ne saurait pas dans quelles
    conditions la trace a été produite.
    """
    target = Path(path)
    records, report = read_trace(target, strict=strict)
    run = ReplayRun(path=target, report=report)
    steps: list[ReplayStep] = []

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
            # Une synthèse sans étape préalable (clôture de budget) crée la sienne.
            index = int(payload.get("step") or 0)
            if not run.final:
                run.final = str(payload.get("text") or "")
            _step(steps, index)
        elif event_type == EventType.ERROR:
            run.errors.append(str(payload.get("message") or payload.get("code") or "erreur"))
        elif event_type == EventType.RUN_FINISHED:
            run.stop_reason = str(payload.get("stop_reason") or "")
            run.ok = bool(payload.get("ok"))
        elif event_type == EventType.TRACE_TRUNCATED:
            run.truncated = True

    run.steps = sorted(steps, key=lambda item: item.index)
    if run.trace_format and run.trace_format > TRACE_FORMAT_VERSION:
        run.errors.append(
            f"trace de format {run.trace_format} > {TRACE_FORMAT_VERSION} : rejeu partiel"
        )
    return run


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
    if run.truncated:
        lines.append("Trace TRONQUEE : la fin de l'execution n'a pas ete enregistree.")
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.replay",
        description="Rejoue une trace d'execution sans agent ni reseau.",
    )
    parser.add_argument("trace", type=Path, help="fichier .jsonl (ou .jsonl.gz deja decompresse)")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="echoue sur une ligne vide ou illisible au lieu de l'ignorer",
    )
    args = parser.parse_args(argv)

    path = args.trace
    if not path.is_file():
        print(f"Trace introuvable : {path}", file=sys.stderr)
        return 2
    try:
        run = replay(path, strict=args.strict)
    except Exception as exc:  # noqa: BLE001 - la CLI rapporte, elle ne plante pas
        print(f"Trace illisible : {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(run.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(format_run(run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
