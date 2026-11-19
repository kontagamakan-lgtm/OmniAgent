"""Reprise contrôlée d'une exécution interrompue, à partir de sa trace.

Rejouer toute une trace pour « revenir » au dernier état est dangereux : un
`fs_write_text`, un clic navigateur ou une commande shell déjà exécutés le
seraient une seconde fois. La reprise rejoue donc **uniquement les outils
déclarés `read_only`** par le registre, avec leurs arguments enregistrés, avant
de donner la main au modèle. Les appels à effet — et les outils inconnus, dont
on ne peut pas prouver l'innocuité — sont refusés, jamais exécutés.

Les observations fraîches sont ajoutées au contexte : le modèle repart de
l'état réellement relu, pas d'un simple souvenir de trace. Par défaut, rien
n'est exécuté : `--resume` affiche le plan ; `--resume --resume-apply` rejoue
les lectures puis lance l'agent.

Les appels d'outils sont classés :

* lecture seule (`fs_read_text`, `fs_list`, `web_search`, ...) : rejoués ;
* à effet (`fs_write_text`, `shell_run`, navigateur, coffre) : signalés comme
  déjà effectués et refusés — le nouveau run doit vérifier l'état avant d'agir.

Une trace terminée normalement n'a pas besoin d'être reprise : le plan le dit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import APP_ID
from core.replay import ReplayRun, replay
from core.tool_registry import ToolRegistry

__all__ = [
    "READ_ONLY_TOOLS",
    "ReplayObservation",
    "ResumeCall",
    "ResumePlan",
    "ResumeReplay",
    "build_plan",
    "format_plan",
    "replay_read_only",
    "resume_prompt",
]

logger = logging.getLogger(f"{APP_ID}.resume")

#: Liste blanche du rejeu. Un outil doit y figurer **et** être déclaré
#: `read_only` dans le registre : un outil ajouté plus tard est donc refusé par
#: défaut, jamais exécuté parce que son drapeau par défaut est permissif.
READ_ONLY_TOOLS = frozenset(
    {
        "fs_read_text",
        "fs_list",
        "fs_search",
        "fs_info",
        "fs_hash",
        "fs_disk_usage",
        "fs_find_duplicates",
        "web_search",
        "web_fetch",
        "web_deep_research",
        "web_search_and_fetch",
        "doc_index_search",
        "doc_index_stats",
        "ocr_image",
        "ocr_status",
        "shell_status",
        "vault_get_entry",
        "vault_list_entries",
        "vault_services",
        "vault_status",
        "vault_totp",
        "vault_generate_password",
    }
)


@dataclass(slots=True)
class ResumeCall:
    """Appel d'outil enregistré dans la trace, avec ses arguments."""

    step: int
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


@dataclass(slots=True)
class ReplayObservation:
    """Résultat d'un rejeu, ou raison explicite du refus."""

    step: int
    tool: str
    ok: bool = False
    text: str = ""
    refused: str = ""


@dataclass(slots=True)
class ResumeReplay:
    """Lectures rejouées et appels à effet écartés."""

    observations: list[ReplayObservation] = field(default_factory=list)

    @property
    def executed(self) -> list[ReplayObservation]:
        return [item for item in self.observations if not item.refused]

    @property
    def refused(self) -> list[ReplayObservation]:
        return [item for item in self.observations if item.refused]


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
    calls: list[ResumeCall] = field(default_factory=list)

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
            arguments = call.get("arguments")
            plan.calls.append(
                ResumeCall(
                    step=step.index,
                    tool=name,
                    arguments=dict(arguments) if isinstance(arguments, dict) else {},
                    ok=bool(call.get("ok", True)),
                )
            )
            if name not in READ_ONLY_TOOLS:
                plan.unsafe.append(line)
    return plan


def replay_read_only(plan: ResumePlan, registry: ToolRegistry) -> ResumeReplay:
    """Exécute les appels de lecture seule, refuse tout le reste.

    Le refus est **par appel** : une trace interrompue après un `fs_write_text`
    reste reprenable, mais ce write n'est jamais rejoué. Un outil absent du
    registre est refusé aussi : sans son drapeau `read_only`, l'exécuter
    reviendrait à supposer qu'il est sans effet.
    """
    outcome = ResumeReplay()
    for call in plan.calls:
        tool = registry.get(call.tool)
        if tool is None:
            outcome.observations.append(
                ReplayObservation(
                    step=call.step,
                    tool=call.tool,
                    refused="outil indisponible : innocuite non prouvable",
                )
            )
            continue
        if (
            call.tool not in READ_ONLY_TOOLS
            or not bool(getattr(tool, "read_only", False))
            or bool(getattr(tool, "dangerous", False))
        ):
            outcome.observations.append(
                ReplayObservation(
                    step=call.step,
                    tool=call.tool,
                    refused="outil a effet de bord : non rejoue",
                )
            )
            continue
        result = registry.dispatch(call.tool, call.arguments)
        outcome.observations.append(
            ReplayObservation(
                step=call.step,
                tool=call.tool,
                ok=result.ok,
                text=registry.observation(result),
            )
        )
    return outcome


def _one_line(text: str, limit: int) -> str:
    """Extrait compact : une observation multi-lignes ne casse pas la liste."""
    compact = " ".join(str(text or "").split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


def resume_prompt(
    plan: ResumePlan, task: str = "", replay: ResumeReplay | None = None
) -> str:
    """Nouvelle demande, enrichie de l'état déjà atteint et des lectures relues."""
    goal = (task or plan.task or "poursuis la tâche interrompue").strip()
    lines = [
        f"Reprise d'une execution interrompue ({plan.run_id or plan.path.name}).",
        f"Tache d'origine : {plan.task or '(inconnue)'}",
        "Etapes deja effectuees (ne pas les refaire aveuglement) :",
    ]
    lines.extend(f"- {item}" for item in plan.completed[:40])
    if replay is not None and replay.executed:
        lines.append(
            "Observations fraiches des lectures rejouees avant cette reprise :"
        )
        for item in replay.executed[:20]:
            status = "ok" if item.ok else "echec"
            lines.append(
                f"- etape {item.step} : {item.tool} ({status}) : "
                f"{_one_line(item.text, 1200)}"
            )
    if replay is not None and replay.refused:
        lines.append(
            "Appels refuses, non rejoues (verifier l'etat avant toute action) :"
        )
        lines.extend(
            f"- etape {item.step} : {item.tool} ({item.refused})"
            for item in replay.refused[:20]
        )
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
    """Rendu console, sans caractère hors cp1252."""
    lines = [
        f"Trace : {plan.path}",
        f"Tache : {plan.task or '(inconnue)'}",
        f"Etat : {'termine' if plan.ok else 'interrompu ou incomplet'}",
        f"Etapes : {plan.steps}, appels d'outils : {len(plan.completed)} "
        f"(lecture seule : {plan.safe_calls})",
        f"Rejeu : {plan.safe_calls} lecture(s) seule(s), "
        f"{len(plan.unsafe)} appel(s) a effet refuse(s)",
    ]
    if plan.unsafe:
        lines.append(f"Appels a effet, refuses au rejeu : {len(plan.unsafe)}")
        lines.extend(f"  - {item}" for item in plan.unsafe[:10])
    if plan.errors:
        lines.append("Erreurs : " + " | ".join(plan.errors[:3]))
    if plan.ok:
        lines.append("Cette trace est terminee : une reprise n'est pas necessaire.")
    else:
        lines.append(
            "Apercu seulement : --resume-apply rejoue les lectures puis lance l'agent."
        )
    return "\n".join(lines)
