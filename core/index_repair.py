"""Réparations sûres et idempotentes de l'index des traces.

Le docteur explique ; ce module répare ce qui peut l'être sans perte : créer le
dossier manquant, reconstruire un index absent, compacter un index dupliqué ou
surdimensionné, rafraîchir taille et chemin, marquer interrompu un run dont le
marqueur est périmé, supprimer un marqueur orphelin. Il ne supprime jamais une
trace.

`apply=False` (défaut) est un aperçu : le plan est calculé et retourné, rien
n'est modifié. `--check --fix` est la seule façon de l'exécuter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.index_doctor import diagnose
from core.run_index import (
    INDEX_NAME,
    RunIndex,
    _run_id_of,
    scan_interrupted,
    workspace_indexes,
)

__all__ = ["format_repair_plan", "repair"]


def repair(
    directory: Path | str,
    *,
    apply: bool = False,
    active_ttl_s: float = 300.0,
) -> list[dict[str, Any]]:
    """Plan de réparation ; l'exécute seulement si `apply`."""
    root = Path(directory)
    actions: list[dict[str, Any]] = []

    def add(code: str, detail: str, run=None) -> None:
        entry = {
            "code": code,
            "detail": detail,
            "applied": False,
        }
        if run is not None:
            try:
                entry["result"] = run()
                entry["applied"] = bool(entry["result"])
            except OSError as exc:
                entry["result"] = False
                entry["error"] = str(exc)
        actions.append(entry)

    def make_root() -> bool:
        """Crée le dossier de traces ; `mkdir` ne renvoie rien à propager."""
        root.mkdir(parents=True, exist_ok=True)
        return True

    if not root.is_dir():
        add(
            "missing_directory",
            f"créer le dossier de traces {root}",
            run=make_root if apply else None,
        )
        if not apply:
            actions[-1]["detail"] = f"créerait le dossier de traces {root}"
        return actions

    issues = diagnose(root, active_ttl_s=active_ttl_s)
    codes = {issue.code for issue in issues}
    directories = workspace_indexes(root)

    if codes & {
        "missing_index",
        "index_unreadable",
        "index_duplicate",
        "index_orphan",
        "index_oversized",
        "index_stale_bytes",
        "index_stale_path",
        "trace_not_indexed",
    }:
        def rebuild_indexes():
            changed = False
            for target in directories:
                index = RunIndex(target)
                if not index.path.exists():
                    index.rebuild(reset=False)
                    changed = True
                    continue
                # Rafraîchit taille et chemin depuis les fichiers, puis compacte :
                # l'index ne garde que des runs présents et à jour.
                entries = index.entries()
                present = {
                    _run_id_of(path): path
                    for path in target.glob("*.jsonl*")
                    if path.is_file() and path.name != INDEX_NAME
                }
                for run_id, record in entries.items():
                    path = present.get(run_id)
                    if path is None:
                        continue
                    try:
                        record.bytes = path.stat().st_size
                    except OSError:
                        continue
                    record.path = str(path)
                    index.append(record)
                index.compact()
                index.rebuild(reset=False)
                changed = True
            return changed

        add(
            "index",
            "rafraîchir puis compacter les index (supprime les entrées orphelines)",
            run=rebuild_indexes if apply else None,
        )

    # Marqueurs : un orphelin n'a plus de trace, un périmé signalle un run tué.
    orphans = [issue for issue in issues if issue.code == "active_orphan"]
    for issue in orphans:
        run_id = issue.message.split(":", 1)[0].strip()
        marker = root / f"{run_id}.active"
        add(
            "active_orphan",
            f"supprimer le marqueur sans trace {marker.name}",
            run=(lambda m=marker: (m.unlink(missing_ok=True), True)[1]) if apply else None,
        )

    stale = [issue for issue in issues if issue.code == "active_stale"]
    if stale:
        def mark_stale():
            return bool(scan_interrupted(root, active_ttl_s=active_ttl_s))

        add(
            "active_stale",
            "marquer interrompus les runs au marqueur périmé",
            run=mark_stale if apply else None,
        )

    return actions


def format_repair_plan(actions: list[dict[str, Any]], *, applied: bool) -> str:
    """Rendu court, sans caractère hors cp1252."""
    if not actions:
        return "Rien a reparer."
    prefix = "applique" if applied else "prevu"
    lines = []
    for action in actions:
        status = prefix
        if applied:
            status = "ok" if action.get("applied") else "echec"
        elif action.get("applied"):
            status = "ok"
        lines.append(f"{status:7} {action['code']} : {action['detail']}")
        error = action.get("error")
        if error:
            lines.append(f"        {error}")
    return "\n".join(lines)
