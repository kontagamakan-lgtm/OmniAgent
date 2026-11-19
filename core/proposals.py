"""Autonomie « proposer puis approuver » : l'agent prépare, l'humain décide.

Le mode autonome exécute après avoir refusé les outils dangereux ; celui-ci ne
touche à rien. Il demande au modèle **une** tâche concrète et son plan, sous
forme de JSON, l'écrit dans `data/autonomy/proposals.jsonl` à l'état `pending`,
et attend une approbation explicite (interface ou `--autonomy-approve`). Une
proposition rejetée n'est jamais exécutée.

Aucun outil n'est appelé ici : seule une génération de texte a lieu.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import APP_ID, Settings
from core.autonomy import (
    MAX_EXCERPT,
    AutonomyJournal,
    format_history,
    goals_path,
    journal_path,
    read_goals,
)
from core.locking import LockTimeout, file_lock

__all__ = [
    "PROPOSALS_NAME",
    "ProposalStore",
    "approve_proposal",
    "finish_proposal",
    "parse_proposal",
    "pending_proposals",
    "propose",
    "proposals_path",
    "reject_proposal",
    "set_proposal_task",
]

logger = logging.getLogger(f"{APP_ID}.proposals")

PROPOSALS_NAME = "proposals.jsonl"
#: Une proposition reste lisible sans recopier tout un raisonnement.
MAX_PROPOSALS = 200
#: Une proposition oubliée devient dangereuse : l'environnement a changé.
DEFAULT_MAX_AGE_DAYS = 14

_SYSTEM = (
    "Tu prépares une proposition de tâche pour un agent Windows. Tu n'exécutes "
    "rien et tu n'appelles aucun outil. Réponds uniquement par un objet JSON."
)

_PROMPT = """Objectifs fixés par l'utilisateur :
---
{goals}
---

Derniers cycles autonomes :
{history}

Propose UNE tâche concrète qui sert ces objectifs et n'a pas déjà été faite.
Réponds par un seul objet JSON :
{{"task": "la tâche en une phrase impérative", "tools": ["outil1"], "risk": "faible|moyen|élevé", "why": "pourquoi elle sert les objectifs"}}
"""


def proposals_path(settings: Settings) -> Path:
    return journal_path(settings).with_name(PROPOSALS_NAME)


def parse_proposal(text: str) -> dict[str, Any]:
    """Extrait la proposition d'une réponse ; retombe sur le texte brut."""
    raw = (text or "").strip()
    candidate = raw
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidate = raw[start : end + 1]
    try:
        data = json.loads(candidate)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    task = " ".join(str(data.get("task") or "").split())[:MAX_EXCERPT]
    if not task:
        task = " ".join(raw.split())[:MAX_EXCERPT]
    tools = data.get("tools")
    if isinstance(tools, str):
        tools = [tools]
    if not isinstance(tools, list):
        tools = []
    risk = str(data.get("risk") or "inconnu").strip().lower()
    if risk not in {"faible", "moyen", "eleve", "élevé", "inconnu"}:
        risk = "inconnu"
    return {
        "task": task,
        "tools": [str(item) for item in tools if str(item).strip()][:12],
        "risk": risk,
        "why": " ".join(str(data.get("why") or "").split())[:400],
    }


class ProposalStore:
    """Journal de propositions, borné, avec changement d'état atomique."""

    def __init__(self, path: Path | str, *, max_entries: int = MAX_PROPOSALS) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))

    def all(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                records.append(data)
        return records

    def append(self, record: dict[str, Any]) -> bool:
        body = {"at": datetime.now(tz=timezone.utc).isoformat(), **record}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self._lock_path()):
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(body, ensure_ascii=False, default=str) + "\n"
                    )
                    handle.flush()
                self._prune_locked()
        except LockTimeout:
            logger.warning("propositions verrouillees : entree non ecrite")
            return False
        except OSError:
            logger.debug("proposition non écrite", exc_info=True)
            return False
        return True

    def _lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def _prune_locked(self) -> None:
        lines = self.all()[-(self.max_entries) :]
        try:
            raw_lines = [
                line
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except OSError:
            return
        if len(raw_lines) <= self.max_entries:
            return
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                for record in lines:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            os.replace(temp, self.path)
        except OSError:
            logger.debug("purge des propositions impossible", exc_info=True)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def find(self, proposal_id: str) -> dict[str, Any] | None:
        for record in self.all():
            if str(record.get("id")) == proposal_id:
                return record
        return None

    def set_state(self, proposal_id: str, state: str) -> dict[str, Any] | None:
        """Change l'état et réécrit le fichier ; `None` si l'identifiant manque."""
        return self.update(proposal_id, state=state)

    def update(self, proposal_id: str, **changes: Any) -> dict[str, Any] | None:
        """Change des champs (état, tâche, run_id) sous verrou.

        La réécriture complète est nécessaire : une proposition est un objet
        unique, pas un flux append-only. Le verrou évite qu'une approbation
        concurrente perde l'édition de l'autre.
        """
        try:
            with file_lock(self._lock_path()):
                return self._update_locked(proposal_id, changes)
        except LockTimeout:
            logger.warning("propositions verrouillees : etat non ecrit")
            return None

    def _update_locked(
        self, proposal_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        records = self.all()
        found: dict[str, Any] | None = None
        for record in records:
            if str(record.get("id")) == proposal_id:
                record.update(changes)
                found = record
                break
        if found is None:
            return None
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            os.replace(temp, self.path)
        except OSError:
            logger.debug("état de proposition non écrit", exc_info=True)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return found

    def expire(self, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> list[str]:
        """Marque `expired` les propositions en attente trop anciennes."""
        if max_age_days <= 0:
            return []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
        expired: list[str] = []
        for record in self.all():
            if record.get("type") != "proposal" or record.get("state") != "pending":
                continue
            created = _parse_time(record.get("at"))
            if created is None or created >= cutoff:
                continue
            if self.update(str(record.get("id")), state="expired"):
                expired.append(str(record.get("id")))
        return expired


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalized_task(task: Any) -> str:
    return " ".join(str(task or "").lower().split())


def propose(settings: Settings, agent: Any) -> tuple[dict[str, Any] | None, str]:
    """Demande une proposition au modèle ; retourne (proposition, erreur)."""
    goals = read_goals(goals_path(settings))
    if not goals:
        return None, "aucun objectif : rien à proposer"
    journal = AutonomyJournal(
        journal_path(settings),
        max_entries=settings.autonomy.journal_max_entries,
        max_bytes=settings.autonomy.journal_max_bytes,
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": _PROMPT.format(
                goals=goals, history=format_history(journal.tail())
            ),
        },
    ]
    try:
        response = agent.llm.complete(messages, tools=None)
    except Exception as exc:  # noqa: BLE001 - une génération ratée n'écrit rien
        return None, f"{type(exc).__name__}: {exc}"
    proposal = parse_proposal(str(getattr(response, "text", "") or ""))
    if not proposal["task"]:
        return None, "réponse vide : aucune proposition"
    store = ProposalStore(proposals_path(settings))
    # Une même tâche proposée deux fois encombre la file et sera approuvée
    # deux fois : la proposition en attente existante fait foi.
    normalized = _normalized_task(proposal["task"])
    for existing in pending_proposals(settings):
        if _normalized_task(existing.get("task")) == normalized:
            return existing, ""
    record = {
        "type": "proposal",
        "id": uuid.uuid4().hex[:8],
        "state": "pending",
        "goals_file": str(goals_path(settings)),
        **proposal,
    }
    if not store.append(record):
        return None, "proposition non enregistrée (fichier inaccessible)"
    return record, ""


def pending_proposals(settings: Settings) -> list[dict[str, Any]]:
    store = ProposalStore(proposals_path(settings))
    store.expire(float(getattr(settings.autonomy, "proposal_max_age_days", 14)))
    return [
        record
        for record in store.all()
        if record.get("type") == "proposal" and record.get("state") == "pending"
    ]


def approve_proposal(settings: Settings, proposal_id: str) -> dict[str, Any] | None:
    """Approuve une proposition encore en attente, sinon `None`.

    Une proposition expirée ou déjà traitée ne peut pas être relancée par une
    approbation tardive : son environnement a pu changer.
    """
    store = ProposalStore(proposals_path(settings))
    record = store.find(proposal_id)
    if record is None or record.get("state") != "pending":
        return None
    return store.update(proposal_id, state="approved")


def reject_proposal(settings: Settings, proposal_id: str) -> dict[str, Any] | None:
    return ProposalStore(proposals_path(settings)).set_state(proposal_id, "rejected")


def set_proposal_task(
    settings: Settings, proposal_id: str, task: str
) -> dict[str, Any] | None:
    """Édite la tâche avant approbation ; refusé hors état `pending`."""
    clean = " ".join(str(task or "").split())[:MAX_EXCERPT]
    if not clean:
        return None
    store = ProposalStore(proposals_path(settings))
    record = store.find(proposal_id)
    if record is None or record.get("state") != "pending":
        return None
    return store.update(proposal_id, task=clean, edited=True)


def finish_proposal(
    settings: Settings,
    proposal_id: str,
    *,
    ok: bool,
    run_id: str = "",
    detail: str = "",
) -> dict[str, Any] | None:
    """Clôt une proposition approuvée : `executed` ou `failed`.

    L'historique conserve le run, le résultat et un détail, pour distinguer une
    approbation d'une exécution réelle.
    """
    store = ProposalStore(proposals_path(settings))
    record = store.find(proposal_id)
    if record is None or record.get("state") != "approved":
        return None
    return store.update(
        proposal_id,
        state="executed" if ok else "failed",
        run_id=run_id,
        ok=bool(ok),
        detail=str(detail)[:300],
        finished_at=datetime.now(tz=timezone.utc).isoformat(),
    )
