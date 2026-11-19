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
from typing import Any, Callable

from config import APP_ID, Settings
from core.autonomy import autonomy_dir

__all__ = [
    "SCHEDULE_NAME",
    "Schedule",
    "load_schedule",
    "sessions_path",
    "should_start",
    "system_idle_seconds",
    "system_on_battery",
    "schedule_path",
]

logger = logging.getLogger(f"{APP_ID}.schedule")

SCHEDULE_NAME = "schedule.json"


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
    return Schedule(
        start_hour=start,
        end_hour=end,
        min_idle_s=max(0, integer("min_idle_s", defaults.min_idle_s)),
        require_ac=bool(raw.get("require_ac", defaults.require_ac)),
        check_interval_s=max(5, integer("check_interval_s", defaults.check_interval_s)),
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


def should_start(
    schedule: Schedule,
    now: datetime,
    *,
    idle_s: float,
    on_battery: bool,
    last_slot: str = "",
) -> tuple[bool, str]:
    """Décide du démarrage, avec la raison du refus (testable sans horloge)."""
    if not schedule.in_window(now):
        return False, "hors fenetre horaire"
    if schedule.slot(now) == last_slot:
        return False, "session deja faite pour cette fenetre"
    if idle_s < schedule.min_idle_s:
        return False, f"machine active (inactivite {int(idle_s)} s < {schedule.min_idle_s} s)"
    if schedule.require_ac and on_battery:
        return False, "machine sur batterie"
    return True, "conditions reunies"


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
) -> list[dict[str, Any]]:
    """Attend la fenêtre puis exécute au plus une session par fenêtre."""
    from core.autonomy import AutonomySession

    schedule = load_schedule(settings)
    results: list[dict[str, Any]] = []
    checks = 0
    while max_checks is None or checks < max_checks:
        checks += 1
        current = now()
        allowed, reason = should_start(
            schedule,
            current,
            idle_s=idle(),
            on_battery=on_battery(),
            last_slot=_last_slot(settings),
        )
        if on_event is not None:
            on_event(f"{current.isoformat()} : {reason}")
        if allowed:
            started = time.monotonic()
            session = AutonomySession(settings, agent)
            records = session.run(cycles=settings.autonomy.max_cycles, interval_s=0)
            summary = {
                "type": "scheduled_session",
                "slot": schedule.slot(current),
                "at": datetime.now(timezone.utc).isoformat(),
                "cycles": len(records),
                "ok": all(record.get("ok") for record in records) if records else False,
                "cost": round(session.cost_used, 4),
                "tokens": session.tokens_used,
                "duration_s": round(time.monotonic() - started, 1),
            }
            _append_session(settings, summary)
            results.append(summary)
            if max_checks is not None:
                break
        if max_checks is not None and checks >= max_checks:
            break
        sleep(schedule.check_interval_s)
    return results
