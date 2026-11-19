"""Mode autonome borné : l'agent choisit et exécute une tâche utile.

Rien ne tourne sans un lancement explicite (bouton « Autonomie » ou
`python app.py --autonomy`) et sans objectifs écrits par l'utilisateur dans
`data/autonomy/goals.md`. Chaque cycle :

1. relit les objectifs ;
2. rappelle au modèle les derniers cycles du journal ;
3. laisse l'agent choisir **une** tâche concrète et l'exécuter avec ses outils ;
4. consigne le résultat dans `data/autonomy/journal.jsonl` (une ligne par
   événement), en plus de la trace habituelle.

Les outils dangereux (shell, suppressions, coffre) sont refusés pendant la
session, puis le confirmateur d'origine est restauré. La session s'arrête
après `max_cycles`, sur demande (`stop`) ou après deux échecs consécutifs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import APP_ID, Settings

__all__ = [
    "GOALS_NAME",
    "JOURNAL_NAME",
    "AutonomyJournal",
    "AutonomySession",
    "autonomy_dir",
    "format_history",
    "goals_path",
    "journal_path",
    "read_goals",
]

logger = logging.getLogger(f"{APP_ID}.autonomy")

GOALS_NAME = "goals.md"
JOURNAL_NAME = "journal.jsonl"
#: Un extrait suffit à comprendre un cycle sans recopier toute la réponse.
MAX_EXCERPT = 600
#: Deux échecs de suite : inutile de continuer à dépenser des tokens.
MAX_CONSECUTIVE_FAILURES = 2
#: Cycles résumés dans l'invite du cycle suivant.
HISTORY_CYCLES = 5

_PROMPT = """Mode autonome OmniAgent.

Objectifs fixés par l'utilisateur (fichier goals.md) :
---
{goals}
---

Derniers cycles déjà effectués (du plus ancien au plus récent) :
{history}

Choisis UNE seule tâche concrète qui sert ces objectifs et qui n'a pas déjà été
faite, puis exécute-la maintenant avec tes outils. Règles :
- ne demande pas de confirmation : les outils dangereux sont refusés ;
- reste dans l'espace de travail autorisé et ne touche pas aux secrets ;
- si les objectifs sont déjà satisfaits, réponds « rien à faire » et explique ;
- termine par un bilan de deux ou trois phrases : ce qui a été fait, et où.
"""


def autonomy_dir(settings: Settings) -> Path:
    """Dossier des objectifs et du journal d'autonomie."""
    return settings.paths.data_dir / "autonomy"


def goals_path(settings: Settings) -> Path:
    """Fichier d'objectifs ; un chemin relatif part de `data/`."""
    configured = settings.autonomy.goals_file.strip()
    if not configured:
        return autonomy_dir(settings) / GOALS_NAME
    path = Path(configured).expanduser()
    return path if path.is_absolute() else settings.paths.data_dir / path


def journal_path(settings: Settings) -> Path:
    return autonomy_dir(settings) / JOURNAL_NAME


def read_goals(path: Path | str) -> str:
    """Objectifs lisibles, ou chaîne vide si le fichier manque/est vide."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def format_history(records: list[dict[str, Any]], limit: int = HISTORY_CYCLES) -> str:
    """Résumé des derniers cycles terminés, pour éviter les répétitions."""
    finished = [record for record in records if record.get("type") == "cycle_finished"]
    lines: list[str] = []
    for record in finished[-limit:]:
        outcome = "ok" if record.get("ok") else "echec"
        excerpt = " ".join(str(record.get("excerpt") or "").split())[:200]
        lines.append(
            f"- cycle {record.get('cycle', '?')} ({record.get('at', '?')}) : "
            f"{outcome} — {excerpt or '(sans bilan)'}"
        )
    return "\n".join(lines) if lines else "(aucun cycle précédent)"


class AutonomyJournal:
    """Journal JSONL des cycles autonomes, borné aux derniers cycles.

    Deux bornes, cohérentes avec la cadence du mode autonome : un nombre
    d'entrées et une taille. Sans elles, une session régulière ferait grossir
    `journal.jsonl` sans fin, comme `runs.index` avant sa compaction.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_entries: int = 200,
        max_bytes: int = 256 * 1024,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))

    def append(self, record: dict[str, Any]) -> bool:
        """Écrit une ligne ; un journal inaccessible ne casse pas la session."""
        body = {"at": datetime.now(tz=timezone.utc).isoformat(), **record}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(body, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.debug("journal d'autonomie non écrit", exc_info=True)
            return False
        self.prune()
        return True

    def prune(self) -> bool:
        """Réécrit le journal avec les derniers cycles, sous les deux bornes.

        La dernière entrée est toujours conservée, même si elle dépasse à elle
        seule la taille maximale : mieux vaut un journal trop gros qu'un cycle
        dont il ne reste aucune trace. Retourne vrai si le fichier a été réécrit.
        """
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) <= self.max_entries and len(raw.encode("utf-8")) <= self.max_bytes:
            return False
        kept = lines[-self.max_entries :]
        total = sum(len(line.encode("utf-8")) + 1 for line in kept)
        while len(kept) > 1 and total > self.max_bytes:
            total -= len(kept[0].encode("utf-8")) + 1
            kept.pop(0)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                for line in kept:
                    handle.write(line + "\n")
            os.replace(temp, self.path)
        except OSError:
            logger.debug("compaction du journal impossible", exc_info=True)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Derniers enregistrements valides, dans l'ordre d'écriture."""
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                records.append(data)
        return records


class AutonomySession:
    """Enchaîne les cycles autonomes sur un agent déjà construit."""

    def __init__(
        self,
        settings: Settings,
        agent: Any,
        *,
        journal: AutonomyJournal | None = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.journal = journal or AutonomyJournal(
            journal_path(settings),
            max_entries=settings.autonomy.journal_max_entries,
            max_bytes=settings.autonomy.journal_max_bytes,
        )
        self.stop = threading.Event()

    def request_stop(self) -> None:
        """Demande l'arrêt : effectif entre deux étapes et entre deux cycles."""
        self.stop.set()
        cancel = getattr(self.agent, "cancel", None)
        if callable(cancel):
            cancel()

    def _deny(self, tool: Any, arguments: Any) -> bool:
        """Refus explicite en autonomie : aucune action dangereuse sans humain."""
        logger.warning(
            "autonomie : outil dangereux refusé (%s)", getattr(tool, "name", "?")
        )
        return False

    def run(
        self,
        *,
        cycles: int | None = None,
        interval_s: float | None = None,
        on_cycle: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Exécute la session et retourne ses enregistrements de journal."""
        cfg = self.settings.autonomy
        total = max(1, int(cycles if cycles is not None else cfg.max_cycles))
        interval = max(
            0.0,
            float(
                interval_s
                if interval_s is not None
                else cfg.interval_minutes * 60.0
            ),
        )
        records: list[dict[str, Any]] = []
        failures = 0
        previous = None
        installed = False
        registry = getattr(self.agent, "registry", None)
        if registry is not None and hasattr(registry, "set_confirm"):
            previous = registry.set_confirm(self._deny)
            installed = True
        try:
            for number in range(1, total + 1):
                if self.stop.is_set():
                    break
                record = self._cycle(number)
                records.append(record)
                if on_cycle is not None:
                    try:
                        on_cycle(record)
                    except Exception:  # noqa: BLE001 - un afficheur ne bloque pas
                        logger.debug("affichage d'un cycle impossible", exc_info=True)
                if record.get("type") == "no_goals":
                    break
                failures = 0 if record.get("ok") else failures + 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "autonomie : %d échecs consécutifs, session arrêtée", failures
                    )
                    break
                if number < total and self.stop.wait(interval):
                    break
        finally:
            # Drapeau, pas `previous is not None` : l'absence de confirmateur
            # avant la session est un état à restaurer aussi, sinon le refus
            # autonome resterait installé pour toutes les tâches suivantes.
            if installed:
                registry.set_confirm(previous)
        return records

    def _cycle(self, number: int) -> dict[str, Any]:
        goals_file = goals_path(self.settings)
        goals = read_goals(goals_file)
        if not goals:
            record = {
                "type": "no_goals",
                "cycle": number,
                "goals_file": str(goals_file),
            }
            self.journal.append(record)
            return record

        self.journal.append(
            {"type": "cycle_started", "cycle": number, "goals_file": str(goals_file)}
        )
        prompt = _PROMPT.format(
            goals=goals, history=format_history(self.journal.tail())
        )
        reset = getattr(self.agent, "reset_cancel", None)
        if callable(reset):
            reset()
        started = time.monotonic()
        ok = False
        stop_reason = ""
        text = ""
        error = ""
        run_id = ""
        try:
            reply = self.agent.run(prompt)
            ok = bool(getattr(reply, "ok", False))
            stop_reason = str(getattr(reply, "stop_reason", "") or "")
            text = str(getattr(reply, "text", "") or "")
            error = str(getattr(reply, "error", "") or "")
            run_id = str(getattr(self.agent, "run_id", "") or "")
        except Exception as exc:  # noqa: BLE001 - un cycle raté n'arrête pas tout
            logger.exception("autonomie : cycle %d en échec", number)
            error = f"{type(exc).__name__}: {exc}"
        record = {
            "type": "cycle_finished",
            "cycle": number,
            "run_id": run_id,
            "ok": ok,
            "stop_reason": stop_reason,
            "duration_s": round(time.monotonic() - started, 2),
            "excerpt": " ".join(text.split())[:MAX_EXCERPT],
            "error": error[:200],
        }
        self.journal.append(record)
        return record
