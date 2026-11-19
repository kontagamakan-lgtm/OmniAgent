"""Diagnostic de cohérence entre `runs.index` et les traces présentes.

`--index-rebuild` répare sans expliquer ; ce module explique. Il ne modifie
rien : il compare ce que l'index raconte (états, tailles, chemins) à ce que le
dossier contient réellement, et signale les marqueurs d'activité orphelins ou
périmés. Les avertissements désignent un entretien utile (compaction,
reconstruction) ; les erreurs signalent un index illisible.

    python -m core.replay --index-doctor [dossier]
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.run_index import INDEX_NAME, RunIndex, _run_id_of, _tail_is_terminal

__all__ = ["IndexIssue", "diagnose", "format_issues"]


@dataclass(slots=True)
class IndexIssue:
    """Un écart constaté, avec son niveau."""

    level: str  # "error" | "warning"
    code: str
    message: str

    @property
    def error(self) -> bool:
        return self.level == "error"

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message}

    def __str__(self) -> str:
        prefix = "ERREUR   " if self.error else "ATTENTION"
        return f"{prefix} {self.code} : {self.message}"


def _trace_files(directory: Path) -> dict[str, Path]:
    try:
        candidates = [
            path
            for path in directory.glob("*.jsonl*")
            if path.is_file() and path.name != INDEX_NAME
        ]
    except OSError:
        return {}
    return {_run_id_of(path): path for path in candidates}


def diagnose(directory: Path | str, *, active_ttl_s: float = 300.0) -> list[IndexIssue]:
    """Compare l'index au dossier ; ne lève jamais, même si tout manque."""
    root = Path(directory)
    if not root.is_dir():
        return [
            IndexIssue(
                "warning",
                "missing_directory",
                f"dossier de traces absent : {root} (rien à diagnostiquer)",
            )
        ]
    index = RunIndex(root)
    if not index.path.exists():
        files = _trace_files(root)
        if not files:
            return []
        return [
            IndexIssue(
                "warning",
                "missing_index",
                f"{len(files)} trace(s) sans index : reconstruction possible",
            )
        ]

    issues: list[IndexIssue] = []
    records = list(index.records())
    try:
        physical = sum(
            1
            for line in index.path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        )
    except OSError:
        physical = len(records)
    unreadable = max(0, physical - len(records))
    if unreadable:
        issues.append(
            IndexIssue(
                "error",
                "index_unreadable",
                f"{unreadable} ligne(s) d'index illisible(s) : reconstruire l'index",
            )
        )

    duplicates = {
        run_id: count for run_id, count in Counter(r.run_id for r in records).items() if count > 1
    }
    if duplicates:
        issues.append(
            IndexIssue(
                "warning",
                "index_duplicate",
                f"{len(duplicates)} run(s) avec plusieurs états : compaction conseillée",
            )
        )

    entries = index.entries()
    files = _trace_files(root)
    for run_id, entry in entries.items():
        path = files.get(run_id)
        if path is None:
            issues.append(
                IndexIssue(
                    "warning",
                    "index_orphan",
                    f"{run_id} : trace absente, entrée d'index périmée",
                )
            )
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if entry.bytes != size:
            issues.append(
                IndexIssue(
                    "warning",
                    "index_stale_bytes",
                    f"{run_id} : index {entry.bytes} o, fichier {size} o",
                )
            )
        if entry.path and Path(entry.path) != path:
            issues.append(
                IndexIssue(
                    "warning",
                    "index_stale_path",
                    f"{run_id} : index {entry.path}, fichier {path}",
                )
            )

    for run_id in files:
        if run_id not in entries:
            issues.append(
                IndexIssue(
                    "warning",
                    "trace_not_indexed",
                    f"{run_id} : trace présente, absente de l'index",
                )
            )

    now = time.time()
    for marker in sorted(root.glob("*.active")):
        run_id = marker.name[: -len(".active")]
        if run_id not in files:
            issues.append(
                IndexIssue(
                    "warning",
                    "active_orphan",
                    f"{run_id} : marqueur d'activité sans trace",
                )
            )
            continue
        try:
            age = now - marker.stat().st_mtime
        except OSError:
            continue
        if age > active_ttl_s and not _tail_is_terminal(files[run_id]):
            issues.append(
                IndexIssue(
                    "warning",
                    "active_stale",
                    f"{run_id} : marqueur périmé et trace sans fin (run interrompu)",
                )
            )

    total_traces = 0
    for path in files.values():
        try:
            total_traces += path.stat().st_size
        except OSError:
            continue
    try:
        index_size = index.path.stat().st_size
    except OSError:
        index_size = 0
    if index_size > max(total_traces * 2, 65_536):
        issues.append(
            IndexIssue(
                "warning",
                "index_oversized",
                f"index {index_size} o pour {total_traces} o de traces : compacter",
            )
        )
    return issues


def format_issues(issues: list[IndexIssue]) -> str:
    """Rendu lisible ; un index sain le dit explicitement."""
    if not issues:
        return "Index cohérent avec les traces."
    return "\n".join(str(issue) for issue in issues)
