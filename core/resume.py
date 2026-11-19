"""Reprise contrôlée d'une exécution interrompue, à partir de sa trace.

Rejouer les outils d'une trace pour « revenir » au dernier état est dangereux :
un `fs_write`, un clic navigateur ou une commande shell déjà exécutés le
seraient une seconde fois. La reprise reconstruit donc un **état** lisible —
tâche, étapes, outils faits, dernier bilan — et le donne au modèle comme
contexte d'une nouvelle exécution. Par défaut, rien n'est exécuté : `--resume`
affiche le plan ; `--resume --resume-apply` lance l'agent avec ce contexte.

Les appels d'outils sont classés :

* lecture seule (`fs_read`, `fs_list`, `web_search`, ...) : ils sont résumés ;
* à effet (`fs_write`, `shell_run`, navigateur, coffre) : signalés comme déjà
  effectués, jamais rejoués par la reprise — le nouveau run doit vérifier l'état
  avant d'agir.

Une trace terminée normalement n'a pas besoin d'être reprise : le plan le dit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from config import APP_ID
from core.replay import ReplayRun, replay

__all__ = [
    "READ_ONLY_TOOLS",
    "ResumePlan",
    "build_plan",
    "format_plan",
    "resume_prompt",
]

logger = logging.getLogger(f"{APP_ID}.resume")

#: Outils dont le rejeu serait sans effet de bord ; ils ne sont pas rejoués
#: pour autant, seulement décrits dans le contexte.
READ_ONLY_TOOLS = frozenset(
    {
        "fs_read",
        "fs_list",
        "fs_search",
        "fs_info",
        "web_search",
        "web_fetch",
        "doc_index_search",
        "doc_index_status",
        "ocr_image",
        "system_info",
    }
)


@dataclass(slots=True)
class ResumePlan:
    """Ce qui a été fait, ce qui est sûr, et le contexte à transmettre."""

    path: Path
    task: str = ""
    ok: bool = False
    interrupted: bool = True
    steps: int = 0
    completed: list[str] = field(default_factory=list)
    unsafe: list[str] = field(default_factory=list)
    final: str = ""
    errors: list[str] = field(default_factory=list)
    run_id: str = ""

    @property
    def safe_calls(self) -> int:
        return len(self.completed) - len(self.unsafe)


def build_plan(path: Path | str, *, key: bytes | None = None) -> ResumePlan:
    """Lit la trace et classe les étapes ; `ReplayExhausted` n'est pas requis."""
    run: ReplayRun = replay(path, key=key)
    plan = ResumePlan(
        path=Path(path),
        task=run.task,
        ok=run.ok,
        interrupted=not run.ok and (run.interrupted or run.truncated or not run.stop_reason),
        steps=len(run.steps),
        final=run.final,
        errors=list(run.errors),
        run_id=run.run_id,
    )
    for step in run.steps:
        for call in step.calls:
            name = str(call.get("tool") or call.get("name") or "?")
            line = f"etape {step.index} : {name}"
            plan.completed.append(line)
            if name not in READ_ONLY_TOOLS:
                plan.unsafe.append(line)
    return plan


def resume_prompt(plan: ResumePlan, task: str = "") -> str:
    """Nouvelle demande, enrichie de l'état déjà atteint."""
    goal = (task or plan.task or "poursuis la tâche interrompue").strip()
    lines = [
        f"Reprise d'une execution interrompue ({plan.run_id or plan.path.name}).",
        f"Tache d'origine : {plan.task or '(inconnue)'}",
        "Etapes deja effectuees (ne pas les refaire aveuglement) :",
    ]
    lines.extend(f"- {item}" for item in plan.completed[:40])
    if plan.unsafe:
        lines.append(
            "Appels a effet deja effectues (a verifier avant toute nouvelle action) :"
        )
        lines.extend(f"- {item}" for item in plan.unsafe[:20])
    if plan.final:
        lines.append(f"Dernier bilan connu : {plan.final[:600]}")
    if plan.errors:
        lines.append("Erreurs vues : " + " | ".join(plan.errors[:4]))
    lines.append(f"Continue et termine cette tache : {goal}")
    return "\n".join(lines)


def format_plan(plan: ResumePlan) -> str:
    """Rendu console, sans caractere hors cp1252."""
    lines = [
        f"Trace : {plan.path}",
        f"Tache : {plan.task or '(inconnue)'}",
        f"Etat : {'termine' if plan.ok else 'interrompu ou incomplet'}",
        f"Etapes : {plan.steps}, appels d'outils : {len(plan.completed)} "
        f"(lecture seule : {plan.safe_calls})",
    ]
    if plan.unsafe:
        lines.append(f"Appels a effet, non rejoues : {len(plan.unsafe)}")
        lines.extend(f"  - {item}" for item in plan.unsafe[:10])
    if plan.errors:
        lines.append("Erreurs : " + " | ".join(plan.errors[:3]))
    if plan.ok:
        lines.append("Cette trace est terminee : une reprise n'est pas necessaire.")
    else:
        lines.append("Apercu seulement : --resume-apply lance l'agent avec ce contexte.")
    return "\n".join(lines)
