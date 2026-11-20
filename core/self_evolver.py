"""Moteur d'auto-évolution borné : diagnostic, veille, forge, leçons.

Le noyau exécutable reste immuable. Tout ce que l'agent améliore vit dans
`evolution_core/` : compétences Python (`skills/`) et prompt d'adaptation
(`prompts/adaptations.md`). Un cycle ne peut écrire que là, jamais dans le
code hôte.

Sécurité, par construction :

* désactivé par défaut (`evolution.enabled = False`) et sans effet tant que
  l'utilisateur ne l'active pas ;
* une seule écriture par cycle au plus, précédée d'un instantané complet de
  `evolution_core/` ; un test en sous-processus doit réussir avant que la
  compétence reste chargée, sinon l'instantané est restauré ;
* imports et appels dangereux refusés par `core.skill_loader` avant chargement ;
* `pip install` n'est possible que si `evolution.allow_pip` est vrai, avec un
  nom de paquet validé ;
* budget de temps, nombre de cycles par jour et intervalle minimal.

Le cycle tourne dans un thread d'arrière-plan quand l'utilisateur est inactif.
Il est aussi déclenchable manuellement (`force_cycle`).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from config import APP_ID, Settings
from core.skill_loader import (
    FORBIDDEN_CALLS,
    FORBIDDEN_IMPORTS,
    SkillError,
    SkillLoader,
    validate_skill_source,
)
from tools.base import FunctionTool, Tool, object_schema, param

__all__ = [
    "CORE_DIR_NAME",
    "SelfEvolver",
    "build_evolution_tools",
    "evolution_core_path",
    "evolution_dir",
    "format_evolution_journal",
    "format_evolution_status",
]

logger = logging.getLogger(f"{APP_ID}.evolution")

CORE_DIR_NAME = "evolution_core"
PROMPT_FILE = "adaptations.md"
JOURNAL_NAME = "journal.jsonl"
STATE_NAME = "state.json"
_MAX_HELP_NOTES = (
    ("Erreurs_Corrigées", "Erreurs corrigées"),
    ("Stratégies_Optimales", "Stratégies optimales"),
)
_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,79}$")

_SKILL_PROMPT = """Tu améliores un agent Python local. Réponds UNIQUEMENT par un objet JSON.

Format exact :
{{"name":"nom_court","description":"phrase","parameters":{{"type":"object","properties":{{}}}},"code":"source Python","test":"script Python de test","pip":[]}}

Contraintes du module :
- il définit NAME, DESCRIPTION, PARAMETERS (schéma objet JSON) et def run(**kwargs) ;
- code autonome, sans placeholder ; stdlib uniquement sauf {forbidden} ;
- interdits : eval, exec, compile, os.system, os.popen, __import__, attributs en __ ;
- `test` est un script autonome qui importe le module par son nom (ex. `import mon_skill as skill`) et utilise des `assert` ; il doit réussir.
- `pip` : liste éventuelle de paquets PyPI ; un paquet non autorisé fait refuser la compétence.

Diagnostic récent :
{diagnosis}

Veille collectée :
{research}
"""

_LESSONS_PROMPT = """Tu tiens le journal d'apprentissage d'un agent local.

D'après le diagnostic et les cycles récents, écris 2 à 4 puces Markdown :
- une erreur récurrente et sa correction ;
- une stratégie plus rapide ou plus fiable ;
- une limite à contourner.
Reste factuel, pas de code, pas de secret, 600 caractères maximum.

Diagnostic :
{diagnosis}

Dernier cycle :
{history}
"""


def evolution_dir(settings: Settings) -> Path:
    """Dossier d'état, journaux, tests et instantanés."""
    return settings.paths.data_dir / "evolution"


def evolution_core_path(settings: Settings) -> Path:
    """Cortex évolutif : à côté de l'exe, ou à la racine du projet en source."""
    configured = str(getattr(settings.evolution, "core_dir", "") or "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return (Path(sys.executable).resolve().parent / CORE_DIR_NAME).resolve()
    return (Path(__file__).resolve().parent.parent / CORE_DIR_NAME).resolve()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(slots=True)
class _State:
    version: str = "1.0.0"
    cycles: int = 0
    day: str = ""
    cycles_today: int = 0
    forced_today: int = 0
    skills: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "_State":
        return cls(
            version=str(payload.get("version") or "1.0.0"),
            cycles=max(0, int(payload.get("cycles") or 0)),
            day=str(payload.get("day") or ""),
            cycles_today=max(0, int(payload.get("cycles_today") or 0)),
            forced_today=max(0, int(payload.get("forced_today") or 0)),
            skills=[str(item) for item in payload.get("skills") or []][:500],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cycles": self.cycles,
            "day": self.day,
            "cycles_today": self.cycles_today,
            "forced_today": self.forced_today,
            "skills": list(self.skills),
        }


class _Journal:
    """Journal JSONL borné (entrées et octets), sans dépendance externe."""

    def __init__(self, path: Path, *, max_entries: int, max_bytes: int) -> None:
        self.path = Path(path)
        self.max_entries = max(10, int(max_entries))
        self.max_bytes = max(4096, int(max_bytes))

    def append(self, record: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"at": _now(), **dict(record)}, ensure_ascii=False) + "\n"
                )
            self._trim()
        except OSError:
            logger.debug("journal d'évolution inécrivable", exc_info=True)

    def records(self, *, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        items: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)) :]:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                items.append(value)
        return items

    def _trim(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if (
            len(lines) <= self.max_entries
            and self.path.stat().st_size <= self.max_bytes
        ):
            return
        kept = lines[-self.max_entries :]
        text = "\n".join(kept) + "\n"
        if len(text.encode("utf-8")) > self.max_bytes:
            text = text[-self.max_bytes :]
            text = text.split("\n", 1)[-1] + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)


def format_evolution_status(status: Mapping[str, Any] | None) -> str:
    """Rendu lisible de l'état du moteur, pour le panneau d'évolution."""
    if not status:
        return "Moteur d'auto-évolution indisponible."
    if not status.get("enabled"):
        return (
            "Auto-évolution désactivée (evolution.enabled = false).\n"
            "Activez-la dans config.json pour autoriser les cycles d'arrière-plan."
        )
    skills = status.get("skills") or []
    lines = [
        f"Version : {status.get('version') or '?'}",
        f"Cycles terminés : {status.get('cycles', 0)}",
    ]
    forced_cap = int(status.get("max_forced_cycles_per_day") or 0)
    if forced_cap:
        lines.append(
            f"Cycles forcés aujourd'hui : {int(status.get('forced_today') or 0)}/{forced_cap}"
        )
    lines.append(f"Compétences chargées : {len(skills)}")
    for skill in skills:
        lines.append(f"  · {skill.get('name')} — {skill.get('description', '')}")
    last = status.get("last_cycle") or {}
    if last:
        outcome = "ok" if last.get("ok") else f"échec ({last.get('error') or '?'})"
        lines.append(f"Dernier cycle : {outcome} · {last.get('duration_ms', '?')} ms")
    if status.get("running"):
        lines.append("Cycle en cours.")
    else:
        lines.append(f"Prochain cycle dans {int(status.get('next_in_seconds') or 0)} s.")
    return "\n".join(lines)


def format_evolution_journal(
    records: list[Mapping[str, Any]], *, limit: int = 40
) -> str:
    """Journal des cycles, du plus récent au plus ancien."""
    blocks: list[str] = []
    for record in reversed(records[-max(1, limit) :]):
        stamp = str(record.get("at") or "")[:16].replace("T", " ")
        kind = str(record.get("type") or "")
        if kind == "cycle_started":
            blocks.append(f"{stamp} · cycle démarré ({record.get('reason', '?')})")
        elif kind == "diagnosis":
            slow = ", ".join(item.get("tool", "?") for item in record.get("slow_tools") or [])
            fail = ", ".join(
                item.get("tool", "?") for item in record.get("failing_tools") or []
            )
            parts = []
            if slow:
                parts.append(f"lents : {slow}")
            if fail:
                parts.append(f"en échec : {fail}")
            blocks.append(f"{stamp} · diagnostic — {' ; '.join(parts) or 'rien à signaler'}")
        elif kind == "research":
            blocks.append(f"{stamp} · veille — {record.get('results', 0)} piste(s)")
        elif kind == "skill_forged":
            if record.get("ok"):
                blocks.append(
                    f"{stamp} · compétence « {record.get('name')} » validée "
                    f"({record.get('test_ms', '?')} ms)"
                )
            else:
                blocks.append(
                    f"{stamp} · compétence refusée : {record.get('error', '?')}"
                )
        elif kind == "forge_skipped":
            blocks.append(
                f"{stamp} · forge ignorée — {record.get('reason', 'diagnostic sans signal')}"
            )
        elif kind == "knowledge":
            blocks.append(f"{stamp} · leçons apprises enregistrées")
        elif kind == "cycle_refused":
            blocks.append(
                f"{stamp} · cycle refusé ({record.get('reason', '?')}) : "
                f"{record.get('error', '?')}"
            )
        elif kind == "cycle_finished":
            if record.get("ok"):
                suffix = (
                    "" if record.get("changed", True) else " · sans changement validé"
                )
                blocks.append(
                    f"{stamp} · cycle réussi{suffix} · {record.get('version', '?')} · "
                    f"{record.get('duration_ms', '?')} ms"
                )
            else:
                blocks.append(
                    f"{stamp} · cycle en échec : {record.get('error', '?')}"
                )
    return "\n".join(blocks) if blocks else "Aucun cycle d'auto-évolution enregistré."


class _CycleAborted(Exception):
    """Le cycle a été interrompu par `stop()` entre deux phases."""


class SelfEvolver:
    """Boucle d'auto-amélioration, sans effet si elle est désactivée."""

    def __init__(
        self,
        settings: Settings,
        loader: SkillLoader,
        *,
        agent: Any = None,
        registry: Any = None,
        brain: Any = None,
        core_dir: str | Path | None = None,
        data_dir: str | Path | None = None,
        clock: Any = None,
        prime_directive: Any = None,
    ) -> None:
        self.settings = settings
        self.loader = loader
        self.agent = agent
        self.registry = registry
        self.brain = brain
        self.prime = prime_directive
        self.core_dir = Path(core_dir).resolve() if core_dir else evolution_core_path(settings)
        self.data_dir = Path(data_dir).resolve() if data_dir else evolution_dir(settings)
        self.backups_dir = self.data_dir / "backups"
        self.tests_dir = self.data_dir / "tests"
        self.prompt_path = self.core_dir / "prompts" / PROMPT_FILE
        self._clock = clock or time.time
        self._journal = _Journal(
            self.data_dir / JOURNAL_NAME,
            max_entries=int(settings.evolution.journal_max_entries),
            max_bytes=int(settings.evolution.journal_max_bytes),
        )
        self._state = self._load_state()
        self._cycle_guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._manual_thread: threading.Thread | None = None
        self._unsubscribe: Any = None
        self._last_activity = self._clock()
        self._started = self._clock()
        self._next_cycle = 0.0
        self._last_cycle: dict[str, Any] = {}
        self._last_refusal = ""
        self.router: Any = None
        self.core_dir.mkdir(parents=True, exist_ok=True)
        (self.core_dir / "skills").mkdir(exist_ok=True)
        self.prompt_path.parent.mkdir(parents=True, exist_ok=True)

    # -- raccordement -------------------------------------------------------

    def attach(self, agent: Any, registry: Any, brain: Any = None) -> None:
        """Raccorde l'agent construit et écoute son activité pour l'inactivité."""
        self.agent = agent
        self.registry = registry
        self.brain = brain if brain is not None else self.brain
        bus = getattr(agent, "bus", None)
        if bus is not None and self._unsubscribe is None:
            self._unsubscribe = bus.subscribe(self._on_event)

    def attach_router(self, router: Any) -> None:
        """Raccorde le routeur multi-LLM pour mesurer et classer les modèles."""
        self.router = router

    def attach_prime(self, prime: Any) -> None:
        """Raccorde la Directive Première à la veille autonome."""
        self.prime = prime

    def _on_event(self, _event: Any) -> None:
        self._last_activity = self._clock()

    def _router_summary(self) -> dict[str, Any]:
        router = self.router
        if router is None:
            return {}
        try:
            return dict(router.summary())
        except Exception:  # noqa: BLE001 - un routeur indisponible ne bloque rien
            logger.debug("résumé du routeur indisponible", exc_info=True)
            return {}

    # -- état ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.evolution.enabled)

    def version_label(self) -> str:
        suffix = "Auto-Générée" if self._state.version != "1.0.0" else "Initiale"
        return f"v{self._state.version} - {suffix}"

    def status(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "enabled": self.enabled,
            "version": self.version_label(),
            "cycles": self._state.cycles,
            "forced_today": self._state.forced_today,
            "max_forced_cycles_per_day": int(
                self.settings.evolution.max_forced_cycles_per_day
            ),
            "last_refusal": self._last_refusal,
            "skills": self.loader.specs(),
            "running": bool(
                (self._thread and self._thread.is_alive())
                or (self._manual_thread and self._manual_thread.is_alive())
            ),
            "idle_seconds": round(max(0.0, now - self._last_activity), 1),
            "next_in_seconds": round(max(0.0, self._next_cycle - now), 1),
            "last_cycle": dict(self._last_cycle),
            "on_disk": len(self.loader.discover()),
        }

    def journal(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._journal.records(limit=limit)

    def prompt_overlay(self) -> str:
        """Extrait borné du prompt d'adaptation, injecté dans le prompt système."""
        try:
            text = self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        limit = max(200, int(self.settings.evolution.max_prompt_chars))
        return text[-limit:]

    # -- planification ------------------------------------------------------

    def start(self) -> bool:
        """Démarre le thread d'arrière-plan ; sans activation, ne fait rien."""
        if not self.enabled:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._started = self._clock()
        self._thread = threading.Thread(
            target=self._loop, name="omniagent-evolution", daemon=True
        )
        self._thread.start()
        self._journal.append({"type": "engine_started", "version": self.version_label()})
        return True

    def stop(self) -> None:
        """Arrête la boucle et joint aussi le cycle manuel éventuel."""
        self._stop.set()
        automatic = self._thread
        manual = self._manual_thread
        self._thread = None
        self._manual_thread = None
        current = threading.current_thread()
        # La boucle automatique est réveillée par l'événement. Le cycle manuel,
        # lui, doit être joint sans délai : sinon il pourrait encore écrire
        # après que le bundle a été fermé.
        for thread, timeout in ((automatic, 5.0), (manual, None)):
            if thread is None or thread is current or not thread.is_alive():
                continue
            if timeout is None:
                thread.join()
                continue
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("thread d'évolution non joint : %s", thread.name)
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001 - désabonnement best effort
                logger.debug("désabonnement du bus impossible", exc_info=True)
            self._unsubscribe = None

    def force_cycle(self) -> bool:
        """Lance un cycle immédiat, refusé au-delà du plafond ou du budget."""
        if not self.enabled or self._stop.is_set():
            return False
        if self._cycle_guard.locked():
            return False
        self._mark_day()
        refusal = self._cap_refusal("manuel") or self._budget_refusal()
        if refusal:
            self._refuse_cycle(reason="manuel", error=refusal)
            return False
        self._last_refusal = ""
        thread = threading.Thread(
            target=self._guarded_cycle,
            kwargs={"reason": "manuel"},
            name="omniagent-evolution-manual",
            daemon=True,
        )
        self._manual_thread = thread
        thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            try:
                if not self._due():
                    continue
                self._guarded_cycle(reason="inactivité")
            except Exception:  # noqa: BLE001 - la boucle survit à un cycle raté
                logger.exception("boucle d'auto-évolution interrompue")

    def _check_stop(self) -> None:
        """Point d'arrêt entre les phases, avant toute écriture suivante."""
        if self._stop.is_set():
            raise _CycleAborted("arrêt demandé")

    def _guarded_cycle(self, *, reason: str) -> dict[str, Any]:
        try:
            return self.run_cycle(reason=reason)
        except Exception:  # noqa: BLE001
            logger.exception("cycle d'auto-évolution non rattrapé")
            return {"ok": False, "error": "cycle non rattrapé"}

    def _due(self) -> bool:
        if not self.enabled or self._cycle_guard.locked():
            return False
        now = self._clock()
        idle = self.settings.evolution.idle_minutes * 60.0
        if now - max(self._last_activity, self._started) < idle:
            return False
        if now < self._next_cycle:
            return False
        return self._state.cycles_today < self.settings.evolution.max_cycles_per_day

    # -- cycle --------------------------------------------------------------

    def run_cycle(self, *, reason: str = "inactivité") -> dict[str, Any]:
        """Exécute un cycle complet : diagnostic, veille, forge, leçons."""
        if not self._cycle_guard.acquire(blocking=False):
            return {"ok": False, "error": "cycle déjà en cours"}
        if self._stop.is_set():
            self._cycle_guard.release()
            return {"ok": False, "error": "cycle annulé (arrêt demandé)"}
        started = self._clock()
        self._mark_day()
        refusal = self._cap_refusal(reason) or self._budget_refusal()
        if refusal:
            self._refuse_cycle(reason=reason, error=refusal)
            self._cycle_guard.release()
            return {"ok": False, "error": refusal}
        self._last_refusal = ""
        self._state.cycles_today += 1
        if reason == "manuel":
            self._state.forced_today += 1
        self._save_state()
        self._journal.append(
            {
                "type": "cycle_started",
                "reason": reason,
                "version": self.version_label(),
            }
        )
        result: dict[str, Any] = {"ok": False}
        try:
            diagnosis = self._diagnose()
            self._journal.append({"type": "diagnosis", **diagnosis})
            self._check_stop()
            research = self._research(diagnosis)
            self._journal.append(
                {
                    "type": "research",
                    "results": len(research),
                    "queries": [item.get("query") for item in research],
                }
            )
            self._check_stop()
            prime_result = self._prime_opportunities(research)
            if prime_result:
                self._journal.append({"type": "prime_directive", **prime_result})
            self._check_stop()
            if diagnosis.get("needs_forge"):
                skill = self._forge_skill(diagnosis, research)
            else:
                skill = None
                self._journal.append(
                    {
                        "type": "forge_skipped",
                        "reason": "diagnostic sans signal (aucun outil lent ou en échec)",
                    }
                )
            self._check_stop()
            lessons = self._optimize_knowledge(diagnosis, skill)
            self._check_stop()
            self._refresh_llm_ranking()
            self._state.cycles += 1
            # La version ne monte que sur un changement validé : une compétence
            # dont le test a réussi et qui est effectivement chargée.
            changed = skill is not None
            if changed:
                self._state.version = self._bump(self._state.version)
            self._state.skills = self.loader.names()
            self._save_state()
            result = {
                "ok": True,
                "skill": skill,
                "changed": changed,
                "lessons_chars": len(lessons),
            }
        except _CycleAborted as exc:
            result = {"ok": False, "error": f"cycle annulé : {exc}"}
        except Exception as exc:  # noqa: BLE001 - un cycle raté est documenté
            logger.exception("cycle d'auto-évolution en échec")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            duration = round((self._clock() - started) * 1000, 1)
            record: dict[str, Any] = {
                "type": "cycle_finished",
                "ok": bool(result.get("ok")),
                "changed": bool(result.get("changed")),
                "duration_ms": duration,
                "version": self.version_label(),
            }
            if result.get("error"):
                record["error"] = str(result["error"])
            if result.get("skill"):
                record["skill"] = result["skill"].get("name")
            self._journal.append(record)
            self._last_cycle = record
            self._next_cycle = (
                self._clock() + self.settings.evolution.interval_minutes * 60.0
            )
            self._cycle_guard.release()
        return result

    def _mark_day(self) -> None:
        today = _today()
        if self._state.day != today:
            self._state.day = today
            self._state.cycles_today = 0
            self._state.forced_today = 0

    def _cap_refusal(self, reason: str) -> str:
        """Refus quand le plafond du jour est atteint, forcé ou automatique."""
        if reason == "manuel":
            limit = int(self.settings.evolution.max_forced_cycles_per_day)
            if limit <= 0:
                return "cycles forcés désactivés (evolution.max_forced_cycles_per_day = 0)"
            if self._state.forced_today >= limit:
                return f"plafond de {limit} cycle(s) forcé(s) par jour atteint"
            return ""
        limit = int(self.settings.evolution.max_cycles_per_day)
        if self._state.cycles_today >= limit:
            return f"plafond de {limit} cycle(s) par jour atteint"
        return ""

    def _budget_refusal(self) -> str:
        """Vérifie le budget global avant toute dépense de tokens.

        Un cycle fait deux appels au modèle : l'estimation d'un run est donc
        comptée deux fois. Un registre illisible refuse plutôt que de dépenser
        à l'aveugle, et le prochain cycle réessaiera.
        """
        from core.budget import check, estimate_run

        calls = 3 if self.prime is not None else 2
        try:
            cost, tokens = estimate_run(self.settings, prompt_tokens=1)
            state = check(
                self.settings,
                pending_cost=cost * calls,
                pending_tokens=tokens * calls,
            )
        except Exception as exc:  # noqa: BLE001 - budget illisible = refus
            logger.warning("budget global illisible", exc_info=True)
            return f"budget global illisible : {type(exc).__name__}"
        return "" if state.allowed else state.reason

    def _refuse_cycle(self, *, reason: str, error: str) -> None:
        self._last_refusal = error
        self._journal.append(
            {"type": "cycle_refused", "reason": reason, "error": error}
        )
        self._last_cycle = {
            "ok": False,
            "error": error,
            "duration_ms": 0.0,
            "version": self.version_label(),
        }
        # Un refus ne doit pas être retenté toutes les 5 s par la boucle.
        self._next_cycle = (
            self._clock() + self.settings.evolution.interval_minutes * 60.0
        )
        self._save_state()

    # -- phase 1 : diagnostic ----------------------------------------------

    def _diagnose(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        if self.registry is not None:
            try:
                stats = dict(self.registry.stats())
            except Exception:  # noqa: BLE001 - un registre sans stats n'empêche rien
                stats = {}
        slow: list[dict[str, Any]] = []
        failing: list[dict[str, Any]] = []
        for name, data in stats.items():
            calls = int(data.get("calls") or 0)
            failures = int(data.get("failures") or 0)
            if calls:
                slow.append(
                    {"tool": name, "avg_ms": round(float(data.get("total_ms") or 0) / calls, 1)}
                )
            if failures:
                failing.append({"tool": name, "failures": failures})
        slow.sort(key=lambda item: float(item["avg_ms"]), reverse=True)
        failing.sort(key=lambda item: int(item["failures"]), reverse=True)
        recent = self._journal.records(limit=80)
        errors = sum(
            1
            for record in recent
            if record.get("type") == "cycle_finished" and not record.get("ok")
        )
        return {
            "slow_tools": slow[:3],
            "failing_tools": failing[:3],
            "skills": len(self.loader),
            "recent_errors": errors,
            "cycles": self._state.cycles,
            # Sans signal, forger une compétence ne ferait que brûler des tokens.
            "needs_forge": bool(failing or slow or errors),
            "llm_router": self._router_summary(),
        }

    # -- phase 2 : veille ---------------------------------------------------

    def _prime_opportunities(
        self, research: list[dict[str, str]]
    ) -> dict[str, Any]:
        """Veille silencieuse de la Directive Première, sans casser le cycle."""
        prime = getattr(self, "prime", None)
        if prime is None:
            return {}
        opportunity_research = list(research)
        if self.settings.evolution.allow_network and self.registry is not None:
            for query in prime.research_queries()[:2]:
                try:
                    result = self.registry.dispatch("web_search", {"query": query})
                except Exception:  # noqa: BLE001 - la veille est optionnelle
                    logger.debug("veille Directive Première indisponible", exc_info=True)
                    continue
                if getattr(result, "ok", False):
                    excerpt = result.to_text(max_chars=900)
                    if excerpt.strip():
                        opportunity_research.append(
                            {"query": query, "excerpt": excerpt}
                        )
        try:
            result = prime.scan(
                self._ask,
                research=opportunity_research,
            )
        except Exception as exc:  # noqa: BLE001 - un cycle reste utile
            logger.warning("veille Directive Première en échec", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": bool(result.get("ok")),
            "added": int(result.get("added") or 0),
            "rejected": int(result.get("rejected") or 0),
        }

    def _research(self, diagnosis: Mapping[str, Any]) -> list[dict[str, str]]:
        if not self.settings.evolution.allow_network or self.registry is None:
            return []
        queries: list[str] = []
        failing = diagnosis.get("failing_tools") or []
        slow = diagnosis.get("slow_tools") or []
        if failing:
            queries.append(f"corriger l'erreur récurrente de l'outil {failing[0]['tool']} en Python")
        if slow:
            queries.append(f"optimiser les performances Python de {slow[0]['tool']}")
        if not queries:
            queries.append("bibliothèque Python stdlib pour automatiser des fichiers locaux")
        found: list[dict[str, str]] = []
        for query in queries[:2]:
            try:
                result = self.registry.dispatch("web_search", {"query": query})
            except Exception:  # noqa: BLE001 - la veille est optionnelle
                logger.debug("veille indisponible", exc_info=True)
                continue
            if not getattr(result, "ok", False):
                continue
            excerpt = result.to_text(max_chars=700)
            if excerpt.strip():
                found.append({"query": query, "excerpt": excerpt})
        return found

    # -- phase 3 : forge ----------------------------------------------------

    def _forge_skill(
        self, diagnosis: Mapping[str, Any], research: list[dict[str, str]]
    ) -> dict[str, Any] | None:
        """Demande une compétence au modèle, la teste, puis la garde ou l'annule."""
        prompt = _SKILL_PROMPT.format(
            forbidden=", ".join(sorted(FORBIDDEN_IMPORTS)),
            diagnosis=json.dumps(diagnosis, ensure_ascii=False)[:2000],
            research=json.dumps(research, ensure_ascii=False)[:2500],
        )
        try:
            payload = self._json_object(self._ask(prompt))
            self._check_stop()
        except _CycleAborted:
            raise
        except Exception as exc:  # noqa: BLE001 - pas de compétence cette fois
            self._journal.append(
                {"type": "skill_forged", "ok": False, "error": f"réponse inutilisable : {exc}"}
            )
            return None
        name = str(payload.get("name") or "").strip().lower()
        code = payload.get("code")
        test = payload.get("test")
        if not _SKILL_NAME.fullmatch(name):
            self._journal.append(
                {"type": "skill_forged", "ok": False, "name": name, "error": "nom invalide"}
            )
            return None
        if not isinstance(code, str) or not isinstance(test, str) or not test.strip():
            self._journal.append(
                {
                    "type": "skill_forged",
                    "ok": False,
                    "name": name,
                    "error": "code ou test manquant",
                }
            )
            return None
        packages = payload.get("pip") or []
        if packages and not self.settings.evolution.allow_pip:
            self._journal.append(
                {
                    "type": "skill_forged",
                    "ok": False,
                    "name": name,
                    "error": "installation de paquets refusée (evolution.allow_pip = false)",
                }
            )
            return None
        try:
            for package in packages if isinstance(packages, list) else ():
                self._install_package(str(package))
        except SkillError as exc:
            self._journal.append(
                {
                    "type": "skill_forged",
                    "ok": False,
                    "name": name,
                    "error": f"paquet refusé : {exc}",
                }
            )
            return None

        try:
            validate_skill_source(code, max_chars=self.settings.evolution.max_skill_chars)
            self._validate_test(test)
        except SkillError as exc:
            self._journal.append(
                {"type": "skill_forged", "ok": False, "name": name, "error": str(exc)}
            )
            return None

        skill_path = self.loader.root / f"{name}.py"
        test_path = self.tests_dir / f"{name}_test.py"
        snapshot = self._snapshot(f"skill-{name}")
        try:
            self._write_core(Path("skills") / f"{name}.py", code)
            self.tests_dir.mkdir(parents=True, exist_ok=True)
            test_path.write_text(test, encoding="utf-8")
            ok, detail, test_ms = self._run_test(test_path)
            if not ok:
                self._restore(snapshot, Path("skills") / f"{name}.py")
                test_path.unlink(missing_ok=True)
                self._journal.append(
                    {
                        "type": "skill_forged",
                        "ok": False,
                        "name": name,
                        "error": f"test en échec : {detail}",
                        "test_ms": test_ms,
                    }
                )
                return None
            try:
                self.loader.load(skill_path)
            except SkillError as exc:
                self._restore(snapshot, Path("skills") / f"{name}.py")
                test_path.unlink(missing_ok=True)
                self._journal.append(
                    {
                        "type": "skill_forged",
                        "ok": False,
                        "name": name,
                        "error": f"chargement refusé : {exc}",
                    }
                )
                return None
            self._state.skills = self.loader.names()
            self._save_state()
            self._journal.append(
                {
                    "type": "skill_forged",
                    "ok": True,
                    "name": name,
                    "test_ms": test_ms,
                    "description": str(payload.get("description") or "")[:160],
                }
            )
            return {"name": name, "test_ms": test_ms}
        except (OSError, subprocess.SubprocessError) as exc:
            self._restore(snapshot, Path("skills") / f"{name}.py")
            self._journal.append(
                {
                    "type": "skill_forged",
                    "ok": False,
                    "name": name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return None
        finally:
            self._prune_backups()

    def _install_package(self, name: str) -> None:
        """Installe un paquet PyPI, uniquement si `allow_pip` et nom valide."""
        clean = str(name or "").strip()
        if not _PACKAGE_NAME.fullmatch(clean):
            raise SkillError(f"nom de paquet invalide : {name!r}")
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    clean,
                ],
                capture_output=True,
                text=True,
                timeout=max(30, int(self.settings.evolution.pip_timeout_s)),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SkillError(f"installation impossible : {type(exc).__name__}: {exc}") from exc
        if completed.returncode != 0:
            detail = (str(completed.stderr or "") + " " + str(completed.stdout or "")).strip()
            raise SkillError(
                f"pip a échoué (code {completed.returncode}) : {' '.join(detail.split())[-200:]}"
            )
        self._journal.append({"type": "package_installed", "name": clean})

    def _validate_test(self, source: str) -> None:
        """Le test est isolé mais reste validé : pas d'accès système direct."""
        max_chars = max(1000, int(self.settings.evolution.max_skill_chars))
        if len(source) > max_chars:
            raise SkillError("test trop long")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise SkillError(f"test invalide ligne {exc.lineno}: {exc.msg}") from exc
        if "assert" not in source:
            raise SkillError("le test doit contenir au moins un assert")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
                if roots & FORBIDDEN_IMPORTS:
                    raise SkillError("import interdit dans le test")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
                    raise SkillError("import interdit dans le test")
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name in FORBIDDEN_CALLS:
                    raise SkillError(f"appel interdit dans le test : {name}()")

    def _run_test(self, test_path: Path) -> tuple[bool, str, float]:
        """Exécute le test dans un sous-processus borné (isolation de crash)."""
        env = dict(os.environ)
        parts = [str(self.loader.root), str(self.core_dir.parent)]
        if env.get("PYTHONPATH"):
            parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(parts)
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, str(test_path)],
                cwd=str(self.core_dir.parent),
                capture_output=True,
                text=True,
                timeout=max(5, int(self.settings.evolution.test_timeout_s)),
                check=False,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0,
            )
        except subprocess.TimeoutExpired:
            return False, "délai de test dépassé", round(
                (time.perf_counter() - started) * 1000, 1
            )
        duration = round((time.perf_counter() - started) * 1000, 1)
        if completed.returncode == 0:
            return True, "", duration
        detail = (str(completed.stderr or "") + " " + str(completed.stdout or "")).strip()
        return False, " ".join(detail.split())[-300:] or "code non nul", duration

    # -- phase 4 : leçons ---------------------------------------------------

    def _optimize_knowledge(
        self, diagnosis: Mapping[str, Any], skill: Mapping[str, Any] | None
    ) -> str:
        history = json.dumps(
            {
                "diagnosis": diagnosis,
                "skill": skill,
                "recent": self._journal.records(limit=8),
            },
            ensure_ascii=False,
        )[:3000]
        lessons = " ".join(
            self._ask(_LESSONS_PROMPT.format(diagnosis=json.dumps(diagnosis)[:1500], history=history))
            .replace("```", "")
            .split()
        )
        self._check_stop()
        lessons = lessons[:1200]
        if not lessons:
            raise RuntimeError("leçons vides")
        entry = f"\n## {_today()} — cycle {self._state.cycles + 1}\n{lessons}\n"
        try:
            existing = self.prompt_path.read_text(encoding="utf-8")
        except OSError:
            existing = "# Adaptations apprises\n"
        limit = max(500, int(self.settings.evolution.max_prompt_chars))
        self._write_core(Path("prompts") / PROMPT_FILE, (existing + entry)[-limit:])
        self._remember_lessons(lessons)
        self._journal.append({"type": "knowledge", "chars": len(lessons)})
        return lessons

    def _refresh_llm_ranking(self) -> None:
        """Recalcule le classement après un cycle et le consigne dans Obsidian."""
        router = self.router
        if router is None:
            return
        try:
            ranking = router.optimize_scores()
        except Exception:  # noqa: BLE001 - le classement ne bloque pas le cycle
            logger.debug("optimisation du classement LLM impossible", exc_info=True)
            return
        top = [
            {
                "model": row.get("model"),
                "score": row.get("score"),
                "successes": row.get("successes"),
                "failures": row.get("failures"),
                "tokens_per_s": row.get("tokens_per_s"),
            }
            for row in ranking[:5]
        ]
        self._journal.append({"type": "llm_ranking", "top": top})
        brain = self.brain
        if brain is None:
            return
        lines = ["# Classement LLM", "", "Scores ajustés par les mesures réelles :", ""]
        for row in ranking[:10]:
            lines.append(
                f"- **{row.get('label')}** ({row.get('provider')}) : score "
                f"{row.get('score')} (base {row.get('base_score')}), "
                f"{row.get('successes')} réussite(s) / {row.get('failures')} échec(s), "
                f"{row.get('tokens_per_s')} tok/s"
            )
        try:
            brain.remember(
                f"Classement LLM — {_today()}",
                "\n".join(lines),
                folder="02_Knowledge",
                tags=["llm", "router"],
                links=[title for title, _label in _MAX_HELP_NOTES],
                source="self_evolver",
            )
        except (OSError, ValueError):
            logger.debug("classement LLM non écrit dans Obsidian", exc_info=True)

    def _remember_lessons(self, lessons: str) -> None:
        brain = self.brain
        if brain is None:
            return
        try:
            if self._state.cycles == 0:
                for title, label in _MAX_HELP_NOTES:
                    if not any(
                        str(hit.get("title")) == title for hit in brain.search(title, limit=5)
                    ):
                        brain.remember(
                            title,
                            f"# {label}\n\nFiche de référence tenue par le moteur "
                            "d'auto-évolution.\n\n## Liens\n- [["
                            + "00_CORTEX_INDEX"
                            + "]]\n",
                            folder="02_Knowledge",
                            tags=["evolution", "lecons"],
                            links=["00_CORTEX_INDEX"],
                            source="self_evolver",
                        )
            brain.remember(
                f"Leçons apprises — {_today()}",
                lessons,
                folder="02_Knowledge",
                tags=["evolution", "lecons"],
                links=[title for title, _label in _MAX_HELP_NOTES],
                source="self_evolver",
            )
        except (OSError, ValueError):
            logger.debug("leçons non écrites dans Obsidian", exc_info=True)

    # -- outils internes ----------------------------------------------------

    def _ask(self, prompt: str) -> str:
        llm = getattr(self.agent, "llm", None)
        if llm is None:
            raise RuntimeError("fournisseur LLM indisponible")
        response = llm.complete(
            [
                {
                    "role": "system",
                    "content": "Tu réponds de façon concise, sans secret, en français.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return str(getattr(response, "text", "") or "")

    @staticmethod
    def _json_object(text: str) -> dict[str, Any]:
        body = str(text or "").strip()
        if body.startswith("```"):
            body = "\n".join(
                line for line in body.splitlines() if not line.strip().startswith("```")
            )
        start = body.find("{")
        end = body.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("aucun objet JSON")
        payload = json.loads(body[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("objet JSON attendu")
        return payload

    # -- fichiers, instantanés et état -------------------------------------

    def _write_core(self, relative: Path, text: str) -> None:
        target = (self.core_dir / relative).resolve()
        if self.core_dir not in target.parents:
            raise SkillError("écriture hors du cortex évolutif refusée")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(str(text), encoding="utf-8")
        os.replace(temporary, target)

    def _snapshot(self, label: str) -> Path:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        clean = _SAFE_LABEL.sub("-", label).strip("-")[:40] or "cycle"
        target = self.backups_dir / f"{stamp}-{clean}"
        suffix = 1
        while target.exists():
            target = self.backups_dir / f"{stamp}-{clean}-{suffix}"
            suffix += 1
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.core_dir, target, dirs_exist_ok=True)
        return target

    def _restore(self, snapshot: Path, relative: Path) -> None:
        """Restaure un fichier du cortex depuis l'instantané, ou le supprime."""
        source = snapshot / relative
        target = (self.core_dir / relative).resolve()
        if self.core_dir not in target.parents:
            return
        try:
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            logger.warning("restauration de %s impossible", relative, exc_info=True)

    def _prune_backups(self) -> None:
        limit = max(1, int(self.settings.evolution.backup_limit))
        try:
            backups = sorted(
                (path for path in self.backups_dir.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
        except OSError:
            return
        for path in backups[:-limit]:
            shutil.rmtree(path, ignore_errors=True)

    def _state_path(self) -> Path:
        return self.data_dir / STATE_NAME

    def _load_state(self) -> _State:
        try:
            payload = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _State()
        return _State.from_dict(payload) if isinstance(payload, dict) else _State()

    def _save_state(self) -> None:
        target = self._state_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(
                json.dumps(self._state.to_dict(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            logger.warning("état d'évolution non enregistré", exc_info=True)

    @staticmethod
    def _bump(version: str) -> str:
        parts = str(version).split(".")
        try:
            parts[-1] = str(int(parts[-1]) + 1)
        except (ValueError, IndexError):
            return "1.0.1"
        return ".".join(parts[:3])


def build_evolution_tools(evolver: SelfEvolver) -> list[Tool]:
    """Trois outils fixes et classés : état, cycle manuel, exécution de compétence."""

    def evolution_status() -> dict[str, Any]:
        return evolver.status()

    def evolution_force_cycle() -> dict[str, Any]:
        started = evolver.force_cycle()
        return {
            "started": started,
            "reason": "" if started else evolver.status().get("last_refusal") or "refusé",
        }

    def evolution_run_skill(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"name": name, "result": evolver.loader.run(name, arguments or {})}

    return [
        FunctionTool(
            "evolution_status",
            "Donne la version auto-générée, les compétences forgées et le dernier "
            "cycle d'auto-évolution.",
            object_schema({}),
            evolution_status,
        ),
        FunctionTool(
            "evolution_force_cycle",
            "Force immédiatement un cycle d'auto-évolution (diagnostic, veille, "
            "forge, leçons). Coûte des tokens et peut écrire dans le cortex.",
            object_schema({}),
            evolution_force_cycle,
            read_only=False,
            dangerous=True,
        ),
        FunctionTool(
            "evolution_run_skill",
            "Exécute une compétence auto-générée du cortex évolutif, par son nom "
            "court (sans préfixe skill_).",
            object_schema(
                {
                    "name": param("string", "Nom court de la compétence"),
                    "arguments": param(
                        "object",
                        "Arguments conformes au schéma de la compétence",
                        default={},
                    ),
                },
                required=["name"],
            ),
            evolution_run_skill,
            read_only=False,
            dangerous=True,
        ),
    ]
