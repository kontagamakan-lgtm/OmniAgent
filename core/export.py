"""Schéma stable et lecteur du bundle d'export public.

`--export` écrivait un objet « format 1 » sans contrat lisible ni lecteur. Le
schéma publié (`docs/export.schema.json`) fixe les champs et leurs types ; ce
module le vérifie sans dépendance (pas de `jsonschema` requis) et résume un
bundle importé. Le bundle ne contient ni tâche, ni réponse, ni arguments
d'outils : l'import ne peut donc pas reconstituer le contenu d'une session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["EXPORT_FORMAT", "SUPPORTED_FORMATS", "import_bundle", "validate_bundle"]

EXPORT_FORMAT = 1
SUPPORTED_FORMATS = frozenset({1})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_bundle(data: Any) -> list[str]:
    """Problèmes de structure ; liste vide si le bundle est valide."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["bundle : objet JSON attendu"]
    version = data.get("export_format")
    if not _is_int(version) or version not in SUPPORTED_FORMATS:
        return [f"export_format inconnu : {version!r}"]
    for name, kind in (
        ("run_id", str),
        ("app_version", str),
        ("model", str),
        ("stop_reason", str),
    ):
        if not isinstance(data.get(name), kind):
            problems.append(f"{name} : {kind.__name__} attendu")
    if not isinstance(data.get("ok"), bool):
        problems.append("ok : booleen attendu")
    for name in ("truncated", "interrupted"):
        if name in data and not isinstance(data.get(name), bool):
            problems.append(f"{name} : booleen attendu")
    if not isinstance(data.get("summary"), dict):
        problems.append("summary : objet attendu")
    if not isinstance(data.get("errors"), list):
        problems.append("errors : liste attendue")
    steps = data.get("steps")
    if not isinstance(steps, list):
        problems.append("steps : liste attendue")
    else:
        for position, step in enumerate(steps):
            if not isinstance(step, dict) or not _is_int(step.get("index")):
                problems.append(f"steps[{position}] : index entier attendu")
                continue
            calls = step.get("calls")
            if not isinstance(calls, list):
                problems.append(f"steps[{position}].calls : liste attendue")
                continue
            for index, call in enumerate(calls):
                if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                    problems.append(
                        f"steps[{position}].calls[{index}] : tool texte attendu"
                    )
    return problems


def import_bundle(source: Path | str) -> dict[str, Any]:
    """Valide un fichier et retourne un résumé ; `ValueError` si invalide."""
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"bundle illisible : {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"bundle n'est pas du JSON : {exc}") from exc
    problems = validate_bundle(data)
    if problems:
        raise ValueError("; ".join(problems))
    calls = sum(len(step.get("calls") or []) for step in data["steps"])
    return {
        "export_format": data["export_format"],
        "run_id": data["run_id"],
        "model": data["model"],
        "ok": data["ok"],
        "stop_reason": data["stop_reason"],
        "steps": len(data["steps"]),
        "tool_calls": calls,
        "summary": data["summary"],
    }
