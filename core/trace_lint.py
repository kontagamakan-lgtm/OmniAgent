"""Vérificateur de format des traces JSONL (format 2).

`read_events` est tolérant par choix : il rend ce qu'il peut et ignore le reste.
Ce module est l'inverse — il juge. Une trace doit satisfaire un contrat :

* un seul `run_started`, en tête, et un seul identifiant d'exécution ;
* une séquence `seq` strictement croissante ;
* chaque `tool_finished` répond à un `tool_started` de la même étape et du même
  outil ; un appel resté ouvert n'est toléré que si la trace est tronquée ;
* un `run_finished` terminal (ou `trace_truncated` s'il n'y a pas la place) ;
* une version de format connue de ce lecteur.

`lint_file` retourne les problèmes ; le code de sortie de la CLI est 1 dès qu'un
problème est une erreur. C'est ce qui rend une évolution de schéma vérifiable :
une trace d'hier doit encore passer, ou le format a changé sans migration.

    python -m core.trace_lint data/logs/runs/<run>.jsonl
    python -m core.trace_lint <trace> --json --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from config import TRACE_FORMAT_VERSION
from core.events import EventType
from core.trace import TraceReadReport, read_trace

__all__ = ["LintIssue", "lint_events", "lint_file", "main"]

#: Types connus du format 2 : tout le reste est une évolution non documentée.
KNOWN_TYPES = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.STEP_STARTED,
        EventType.LLM_REQUEST,
        EventType.LLM_RESPONSE,
        EventType.LLM_RETRY,
        EventType.STREAM_CHUNK,
        EventType.CONTEXT_TRIMMED,
        EventType.TOOL_STARTED,
        EventType.TOOL_FINISHED,
        EventType.THOUGHT,
        EventType.FINAL,
        EventType.ERROR,
        EventType.RUN_FINISHED,
        EventType.TRACE_TRUNCATED,
    }
)

_TERMINAL = (EventType.RUN_FINISHED, EventType.TRACE_TRUNCATED)


@dataclass(slots=True)
class LintIssue:
    """Un défaut constaté, avec le niveau qui décide du code de sortie."""

    level: str  # "error" | "warning"
    code: str
    message: str
    seq: int = 0

    @property
    def error(self) -> bool:
        return self.level == "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "seq": self.seq,
        }

    def __str__(self) -> str:
        prefix = "ERREUR" if self.error else "AVERTISSEMENT"
        where = f" (seq {self.seq})" if self.seq else ""
        return f"[{prefix}] {self.code}{where}: {self.message}"


def lint_events(
    records: Sequence[dict[str, Any]],
    *,
    report: TraceReadReport | None = None,
) -> list[LintIssue]:
    """Contrôle une trace déjà lue ; ne lève pas, retourne les problèmes."""
    issues: list[LintIssue] = []

    def error(code: str, message: str, seq: Any = 0) -> None:
        issues.append(LintIssue("error", code, message, int(seq or 0)))

    def warn(code: str, message: str, seq: Any = 0) -> None:
        issues.append(LintIssue("warning", code, message, int(seq or 0)))

    if not records:
        error("empty_trace", "aucun événement lisible")
        return issues

    types = [str(record.get("type") or "") for record in records]
    started = [index for index, name in enumerate(types) if name == EventType.RUN_STARTED]
    finished = [index for index, name in enumerate(types) if name == EventType.RUN_FINISHED]
    truncated = EventType.TRACE_TRUNCATED in types

    if not started:
        error("missing_run_started", "aucun run_started : la trace n'a pas de manifeste")
    elif len(started) > 1:
        error("duplicate_run_started", f"{len(started)} run_started dans la même trace")
    elif started[0] != 0:
        error("run_started_not_first", "run_started doit ouvrir la trace")

    if len(finished) > 1:
        error("duplicate_run_finished", f"{len(finished)} run_finished dans la même trace")
    if not finished and not truncated:
        error(
            "missing_run_finished",
            "aucun run_finished : exécution interrompue ou fichier encore ouvert",
        )
    if finished and finished[-1] != len(records) - 1:
        after = types[finished[-1] + 1 :]
        error(
            "events_after_run_finished",
            f"{len(after)} événement(s) après run_finished : {', '.join(after[:3])}",
            records[finished[-1]].get("seq"),
        )

    ids = {str(record.get("run_id") or "") for record in records}
    if "" in ids:
        error("missing_run_id", "au moins un événement n'a pas de run_id")
    if len(ids - {""}) > 1:
        error("mixed_run_ids", f"plusieurs run_id dans le même fichier : {sorted(ids)}")

    last_seq = -1
    for record in records:
        seq = record.get("seq")
        if not isinstance(seq, int):
            warn("invalid_seq", f"seq non entier : {seq!r}")
            continue
        if seq <= last_seq:
            error("seq_not_increasing", f"seq {seq} après {last_seq}", seq)
        last_seq = max(last_seq, seq)
        if not record.get("timestamp"):
            warn("missing_timestamp", "événement sans horodatage", seq)

    # Appariement des outils, par étape et par nom : un `tool_finished` orphelin
    # signale une écriture fautive, pas une exécution normale.
    open_calls: dict[tuple[int, str], int] = {}
    for record in records:
        name = str(record.get("type") or "")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if name not in (EventType.TOOL_STARTED, EventType.TOOL_FINISHED):
            continue
        key = (int(payload.get("step") or 0), str(payload.get("tool") or ""))
        if name == EventType.TOOL_STARTED:
            open_calls[key] = open_calls.get(key, 0) + 1
            continue
        if open_calls.get(key):
            open_calls[key] -= 1
            if not open_calls[key]:
                del open_calls[key]
        else:
            error(
                "tool_finished_without_start",
                f"tool_finished sans tool_started pour {key[1]!r} à l'étape {key[0]}",
                record.get("seq"),
            )
    for (step, tool), count in sorted(open_calls.items()):
        message = (
            f"{count} appel(s) de {tool!r} à l'étape {step} sans tool_finished"
        )
        if truncated:
            warn("tool_started_without_finish", message)
        else:
            error("tool_started_without_finish", message)

    for record in records:
        name = str(record.get("type") or "")
        if name not in KNOWN_TYPES:
            warn("unknown_event_type", f"type inconnu {name!r}", record.get("seq"))

    if started:
        payload = records[started[0]].get("payload")
        payload = payload if isinstance(payload, dict) else {}
        version = int(payload.get("trace_format") or 0)
        if version > TRACE_FORMAT_VERSION:
            error(
                "newer_trace_format",
                f"format {version} > {TRACE_FORMAT_VERSION} : lecteur trop ancien",
            )
        elif not version:
            warn("missing_trace_format", "manifeste sans version de format")

    if report is not None and report.ignored:
        message = f"{report.ignored} ligne(s) illisible(s) sur {report.lines}"
        if report.truncated_final:
            warn("unreadable_final_line", message)
        else:
            error("unreadable_lines", message)
    return issues


def lint_file(
    path: Path | str, *, strict: bool = False
) -> tuple[list[LintIssue], TraceReadReport]:
    """Lit puis contrôle une trace ; `strict` fait échouer la lecture elle-même."""
    try:
        records, report = read_trace(path, strict=strict)
    except Exception as exc:  # noqa: BLE001 - un lecteur rapporte, il ne plante pas
        issue = LintIssue("error", "unreadable_trace", f"{type(exc).__name__}: {exc}")
        return [issue], TraceReadReport()
    return lint_events(records, report=report), report


def format_issues(issues: Sequence[LintIssue], report: TraceReadReport | None = None) -> str:
    """Rendu lisible, sans caractère hors cp1252."""
    lines = [str(issue) for issue in issues]
    if report is not None:
        lines.append(
            f"{report.events} evenement(s) lus, {report.ignored} ligne(s) ignoree(s)"
        )
    errors = sum(1 for issue in issues if issue.error)
    warnings = len(issues) - errors
    lines.append(f"{errors} erreur(s), {warnings} avertissement(s)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.trace_lint",
        description="Verifie le format d'une trace d'execution.",
    )
    parser.add_argument("trace", type=Path, help="fichier .jsonl ou .jsonl.gz")
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="traite une ligne vide ou illisible comme une erreur de lecture",
    )
    args = parser.parse_args(argv)

    if not args.trace.is_file():
        print(f"Trace introuvable : {args.trace}", file=sys.stderr)
        return 2

    issues, report = lint_file(args.trace, strict=args.strict)
    if args.json:
        print(
            json.dumps(
                {
                    "trace": str(args.trace),
                    "report": report.as_dict(),
                    "issues": [issue.as_dict() for issue in issues],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Trace : {args.trace}")
        print(format_issues(issues, report))
    return 1 if any(issue.error for issue in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
