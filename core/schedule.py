"""Planification légère des sessions autonomes.

Le mode autonome ne tourne que sur lancement. Ce module ajoute une fenêtre
horaire, une exigence d'inactivité et, sous Windows, une exigence de secteur :
une session nocturne ne doit ni gêner l'utilisateur ni vider la batterie.

`autonomy/schedule.json` (facultatif) :

    {"start_hour": 1, "end_hour": 6, "min_idle_s": 300,
     "require_ac": true, "check_interval_s": 60}

Une seule session par fenêtre (par date de début) : le fichier
`autonomy/sessions.jsonl` garde l'historique et sert de verrou logique. Le
verrou inter-processus réel reste celui des journaux (`core.locking`), et une
session déjà en cours ne peut pas être doublée grâce à `AutonomySession`.
"""

from __future__ import annotations

import ctypes
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from config import APP_ID, Settings
from core.autonomy import autonomy_dir

__all__ = [
    "CATCH_UP_MODES",
    "SCHEDULE_NAME",
    "Schedule",
    "StartDecision",
    "load_schedule",
    "missed_slot",
    "plan_start",
    "sessions_path",
    "should_start",
    "system_idle_seconds",
    "system_on_battery",
    "schedule_path",
    "window_end",
]

logger = logging.getLogger(f"{APP_ID}.schedule")

SCHEDULE_NAME = "schedule.json"

#: `none` : une fenêtre manquée est abandonnée. `latest` : la dernière fenêtre
#: manquée est rattrapée une fois, si elle n'est pas trop ancienne.
CATCH_UP_MODES = ("none", "latest")


def schedule_path(settings: Settings) -> Path:
    return autonomy_dir(settings) / SCHEDULE_NAME


def sessions_path(settings: Settings) -> Path:
    return autonomy_dir(settings) / "sessions.jsonl"


@dataclass(slots=True)
class Schedule:
    """Fenêtre et conditions d'une session planifiée."""

    start_hour: int = 1
    end_hour: int = 6
    min_idle_s: int = 300
    require_ac: bool = True
    check_interval_s: int = 60
    #: Politique de rattrapage d'une fenêtre manquée (réveil, session
    #: interrompue, poste éteint).
    catch_up: Literal["none", "latest"] = "latest"
    #: Au-delà de cet âge depuis la fin de la fenêtre, un rattrapage n'a plus
    #: de sens : l'environnement a changé et la tâche serait trompeuse.
    catch_up_hours: float = 18.0

    def in_window(self, now: datetime) -> bool:
        hour = now.hour
        if self.start_hour == self.end_hour:
            return True
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def slot(self, now: datetime) -> str:
        """Identifiant de la fenêtre : la session ne doit partir qu'une fois."""
        if self.start_hour == self.end_hour:
            return now.date().isoformat()
        if self.start_hour < self.end_hour or now.hour >= self.start_hour:
            return now.date().isoformat()
        # Après minuit mais avant la fin d'une fenêtre qui a commencé la veille.
        return (now.date() - timedelta(days=1)).isoformat()


def load_schedule(settings: Settings) -> Schedule:
    """Fichier absent ou invalide : fenêtre par défaut, jamais de plantage."""
    defaults = Schedule()
    try:
        raw = json.loads(schedule_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(raw, dict):
        return defaults
    def integer(name: str, fallback: int) -> int:
        try:
            return int(raw.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    start = max(0, min(23, integer("start_hour", defaults.start_hour)))
    end = max(0, min(24, integer("end_hour", defaults.end_hour)))
    catch_up = str(raw.get("catch_up", defaults.catch_up)).strip().lower()
    if catch_up not in CATCH_UP_MODES:
        catch_up = defaults.catch_up
    try:
        catch_up_hours = float(raw.get("catch_up_hours", defaults.catch_up_hours))
    except (TypeError, ValueError):
        catch_up_hours = defaults.catch_up_hours
    return Schedule(
        start_hour=start,
        end_hour=end,
        min_idle_s=max(0, integer("min_idle_s", defaults.min_idle_s)),
        require_ac=bool(raw.get("require_ac", defaults.require_ac)),
        check_interval_s=max(5, integer("check_interval_s", defaults.check_interval_s)),
        catch_up=catch_up,  # type: ignore[arg-type]
        catch_up_hours=min(168.0, max(0.0, catch_up_hours)),
    )


def _last_slot(settings: Settings) -> str:
    """Dernière fenêtre déjà honorée, ou chaîne vide."""
    try:
        lines = sessions_path(settings).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "scheduled_session":
            return str(data.get("slot") or "")
    return ""


@dataclass(slots=True)
class StartDecision:
    """Démarrage décidé, avec la fenêtre à enregistrer et son origine."""

    run: bool
    slot: str = ""
    #: Vrai quand la session rattrape une fenêtre finie, hors fenêtre courante.
    catch_up: bool = False
    reason: str = ""


def window_end(
    schedule: Schedule, slot: str, *, tzinfo: Any = None
) -> datetime | None:
    """Fin de la fenêtre `slot` (identifiant = date de début), ou `None`."""
    try:
        day = datetime.fromisoformat(f"{slot}T00:00:00")
    except ValueError:
        return None
    if tzinfo is not None:
        day = day.replace(tzinfo=tzinfo)
    if schedule.start_hour == schedule.end_hour:
        return day + timedelta(days=1)
    # Une fenêtre qui commence plus tard qu'elle ne finit se termine le
    # lendemain (22 h -> 6 h).
    days = 1 if schedule.start_hour > schedule.end_hour else 0
    return day + timedelta(days=days, hours=schedule.end_hour)


def missed_slot(schedule: Schedule, now: datetime) -> str:
    """Identifiant de la dernière fenêtre *terminée* avant `now`.

    Sert au rattrapage : `slot(now)` désigne la fenêtre courante ou à venir,
    pas celle qui vient de se terminer. Vide si aucune fenêtre identifiable.
    """
    if schedule.start_hour == schedule.end_hour:
        return schedule.slot(now)
    for offset in (0, 1, 2):
        candidate = (now.date() - timedelta(days=offset)).isoformat()
        end = window_end(schedule, candidate, tzinfo=now.tzinfo)
        if end is not None and end <= now:
            return candidate
    return ""


def plan_start(
    schedule: Schedule,
    now: datetime,
    *,
    idle_s: float,
    on_battery: bool,
    last_slot: str = "",
) -> StartDecision:
    """Décide du démarrage, y compris le rattrapage d'une fenêtre manquée.

    Le verrou anti-double exécution reste `last_slot` : une fenêtre déjà
    honorée, courante ou rattrapée, est refusée. Une fenêtre rattrapée est
    enregistrée avec son propre identifiant, donc elle ne peut pas repartir.
    """
    if schedule.in_window(now):
        slot = schedule.slot(now)
        if slot == last_slot:
            return StartDecision(False, slot, False, "session deja faite pour cette fenetre")
        if idle_s < schedule.min_idle_s:
            return StartDecision(
                False,
                slot,
                False,
                f"machine active (inactivite {int(idle_s)} s < {schedule.min_idle_s} s)",
            )
        if schedule.require_ac and on_battery:
            return StartDecision(False, slot, False, "machine sur batterie")
        return StartDecision(True, slot, False, "conditions reunies")

    if schedule.catch_up == "none":
        return StartDecision(False, "", False, "hors fenetre horaire")
    if idle_s < schedule.min_idle_s:
        return StartDecision(
            False,
            "",
            False,
            f"machine active (inactivite {int(idle_s)} s < {schedule.min_idle_s} s)",
        )
    if schedule.require_ac and on_battery:
        return StartDecision(False, "", False, "machine sur batterie")
    slot = missed_slot(schedule, now)
    if not slot:
        return StartDecision(False, "", False, "hors fenetre horaire")
    if slot == last_slot:
        return StartDecision(False, slot, False, "session deja faite pour cette fenetre")
    end = window_end(schedule, slot, tzinfo=now.tzinfo)
    if end is None:
        return StartDecision(False, "", False, "hors fenetre horaire")
    age_hours = (now - end).total_seconds() / 3600.0
    if age_hours > schedule.catch_up_hours:
        return StartDecision(
            False,
            slot,
            False,
            f"fenetre manquee trop ancienne ({age_hours:.1f} h > {schedule.catch_up_hours:.0f} h)",
        )
    return StartDecision(True, slot, True, f"rattrapage de la fenetre {slot}")


def should_start(
    schedule: Schedule,
    now: datetime,
    *,
    idle_s: float,
    on_battery: bool,
    last_slot: str = "",
) -> tuple[bool, str]:
    """Décide du démarrage, avec la raison du refus (testable sans horloge)."""
    decision = plan_start(
        schedule, now, idle_s=idle_s, on_battery=on_battery, last_slot=last_slot
    )
    return decision.run, decision.reason


def system_idle_seconds() -> float:
    """Inactivité clavier/souris sous Windows ; 0 ailleurs (donc refus)."""
    if __import__("os").name != "nt":
        return 0.0
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, float(elapsed) / 1000.0)
    except Exception:  # noqa: BLE001 - une sonde indisponible ne bloque pas
        return 0.0


def system_on_battery() -> bool:
    """Vrai si Windows rapporte une batterie non branchée ; faux ailleurs."""
    if __import__("os").name != "nt":
        return False
    try:
        class POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("Reserved1", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32),
                ("BatteryFullLifeTime", ctypes.c_uint32),
            ]

        status = POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return status.ACLineStatus == 0
    except Exception:  # noqa: BLE001
        return False


def _append_session(settings: Settings, record: dict[str, Any]) -> None:
    from core.locking import LockTimeout, file_lock

    path = sessions_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_name(f"{path.name}.lock")):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except (OSError, LockTimeout):
        logger.debug("historique de session planifiee non ecrit", exc_info=True)


def run_scheduled(
    settings: Settings,
    agent: Any,
    *,
    max_checks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc).astimezone(),
    idle: Callable[[], float] = system_idle_seconds,
    on_battery: Callable[[], bool] = system_on_battery,
    on_event: Callable[[str], None] | None = None,
    notify: Callable[[str, str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Attend la fenêtre puis exécute au plus une session par fenêtre."""
    from core.autonomy import AutonomySession
    from core.notify import notify_windows

    schedule = load_schedule(settings)
    results: list[dict[str, Any]] = []
    checks = 0
    while max_checks is None or checks < max_checks:
        checks += 1
        current = now()
        decision = plan_start(
            schedule,
            current,
            idle_s=idle(),
            on_battery=on_battery(),
            last_slot=_last_slot(settings),
        )
        if on_event is not None:
            on_event(f"{current.isoformat()} : {decision.reason}")
        if decision.run:
            started = time.monotonic()
            session = AutonomySession(settings, agent)
            records = session.run(cycles=settings.autonomy.max_cycles, interval_s=0)
            summary = {
                "type": "scheduled_session",
                # La fenêtre enregistrée est celle qui a effectivement tourné :
                # celle du rattrapage diffère de la fenêtre courante. C'est ce
                # slot qui empêche la seconde exécution.
                "slot": decision.slot,
                "catch_up": decision.catch_up,
                "at": datetime.now(timezone.utc).isoformat(),
                "cycles": len(records),
                "ok": all(record.get("ok") for record in records) if records else False,
                "cost": round(session.cost_used, 4),
                "tokens": session.tokens_used,
                "duration_s": round(time.monotonic() - started, 1),
            }
            _append_session(settings, summary)
            results.append(summary)
            if getattr(settings.autonomy, "notify_on_session", True):
                notifier = notify or notify_windows
                message = (
                    f"Session {'terminee' if summary['ok'] else 'en echec'} : "
                    f"{summary['cycles']} cycle(s), {summary['cost']:.4f} USD, "
                    f"{summary['duration_s']} s"
                )
                try:
                    if not notifier("OmniAgent - session planifiee", message):
                        logger.info(
                            "notification de fin de session non affichee "
                            "(toast indisponible)"
                        )
                except Exception:  # noqa: BLE001 - une notification ne bloque pas
                    logger.debug("notification de fin de session impossible", exc_info=True)
            if max_checks is not None:
                break
        if max_checks is not None and checks >= max_checks:
            break
        sleep(schedule.check_interval_s)
    return results
