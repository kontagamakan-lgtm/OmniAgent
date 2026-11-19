"""Budget global persistant : plafonds jour/semaine/mois et projection.

Les plafonds d'autonomie vivaient le temps d'une session : un redémarrage
remettait les compteurs à zéro. Ce module tient un registre JSONL
(`data/budget/ledger.jsonl`, une ligne par run terminé) et calcule l'usage de la
journée, de la semaine ISO et du mois en cours.

Le budget est vérifié **avant** chaque appel au modèle (et avant le run) : un
dépassement arrête net avec `stop_reason = "budget"`, sans lancer la requête
suivante. Les plafonds à 0 sont sans limite ; sans plafond, le registre sert
quand même de jauge et de projection.

    python app.py --budget
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import APP_ID, Settings
from core.cost import estimate_cost, load_prices
from core.locking import LockTimeout, file_lock

__all__ = [
    "BudgetState",
    "Usage",
    "build_budget_recorder",
    "check",
    "format_state",
    "ledger_path",
    "record_run",
    "run_cost",
    "usage",
]

logger = logging.getLogger(f"{APP_ID}.budget")

LEDGER_NAME = "ledger.jsonl"
MAX_ENTRIES = 5000


def ledger_path(settings: Settings) -> Path:
    return settings.paths.data_dir / "budget" / LEDGER_NAME


@dataclass(slots=True)
class Usage:
    """Consommation agrégée sur les fenêtres courantes (UTC)."""

    day_cost: float = 0.0
    week_cost: float = 0.0
    month_cost: float = 0.0
    day_tokens: int = 0
    month_tokens: int = 0
    runs_today: int = 0
    runs_month: int = 0
    month_average_run_cost: float = 0.0


@dataclass(slots=True)
class BudgetState:
    """Décision et chiffres pour l'interface ou la CLI."""

    allowed: bool = True
    reason: str = ""
    usage: Usage = field(default_factory=Usage)
    limits: dict[str, float] = field(default_factory=dict)
    projection_month_cost: float = 0.0

    @property
    def remaining(self) -> float:
        """Marge restante la plus contraignante, en USD (`inf` si aucune)."""
        values: list[float] = []
        for name in ("day", "week", "month"):
            limit = float(self.limits.get(name) or 0.0)
            if limit > 0:
                values.append(limit - float(getattr(self.usage, f"{name}_cost")))
        return min(values) if values else float("inf")


def _parse_at(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _read(settings: Settings) -> list[dict[str, Any]]:
    path = ledger_path(settings)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            entries.append(record)
    return entries


def _append(settings: Settings, record: dict[str, Any]) -> bool:
    path = ledger_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(".lock")):
            entries = _read(settings)
            entries.append(record)
            if len(entries) > MAX_ENTRIES:
                entries = entries[-MAX_ENTRIES:]
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries),
                encoding="utf-8",
            )
            temporary.replace(path)
    except (OSError, LockTimeout):
        logger.warning("budget non enregistre", exc_info=True)
        return False
    return True


def run_cost(
    settings: Settings,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    model: str = "",
) -> float:
    """Coût estimé d'un run avec la table et les tarifs configurés."""
    cfg = settings.llm
    return estimate_cost(
        model or cfg.model,
        prompt_tokens,
        completion_tokens,
        prices=load_prices(settings),
        prompt_price=float(getattr(cfg, "price_prompt_per_million", 0.0) or 0.0),
        completion_price=float(getattr(cfg, "price_completion_per_million", 0.0) or 0.0),
    )


def record_run(
    settings: Settings,
    *,
    run_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Ajoute un run au registre, avec son coût estimé."""
    cost = run_cost(settings, prompt_tokens, completion_tokens, model=model)
    tokens = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
    record = {
        "at": (at or datetime.now(tz=timezone.utc)).isoformat(),
        "run_id": str(run_id),
        "model": str(model),
        "prompt_tokens": max(0, int(prompt_tokens)),
        "completion_tokens": max(0, int(completion_tokens)),
        "tokens": tokens,
        "cost": cost,
    }
    return record if _append(settings, record) else None


def usage(
    entries: list[dict[str, Any]], now: datetime | None = None
) -> Usage:
    """Somme les entrées des fenêtres jour / semaine ISO / mois courants."""
    moment = now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    day = moment.date()
    week = moment.isocalendar()[:2]
    month = (moment.year, moment.month)
    result = Usage()
    month_costs: list[float] = []
    for entry in entries:
        stamp = _parse_at(entry.get("at"))
        if stamp is None:
            continue
        cost = float(entry.get("cost") or 0.0)
        tokens = int(entry.get("tokens") or 0)
        local = stamp.astimezone(moment.tzinfo)
        if (local.year, local.month) == month:
            result.month_cost += cost
            result.month_tokens += tokens
            result.runs_month += 1
            month_costs.append(cost)
        if local.isocalendar()[:2] == week:
            result.week_cost += cost
        if local.date() == day:
            result.day_cost += cost
            result.day_tokens += tokens
            result.runs_today += 1
    result.month_cost = round(result.month_cost, 4)
    result.week_cost = round(result.week_cost, 4)
    result.day_cost = round(result.day_cost, 4)
    if month_costs:
        result.month_average_run_cost = round(sum(month_costs) / len(month_costs), 4)
    return result


def _limits(settings: Settings) -> dict[str, float]:
    cfg = getattr(settings, "budget", None)
    if cfg is None:
        return {}
    return {
        "day": float(getattr(cfg, "daily_cost", 0.0) or 0.0),
        "week": float(getattr(cfg, "weekly_cost", 0.0) or 0.0),
        "month": float(getattr(cfg, "monthly_cost", 0.0) or 0.0),
        "day_tokens": float(getattr(cfg, "daily_tokens", 0) or 0),
        "month_tokens": float(getattr(cfg, "monthly_tokens", 0) or 0),
    }


def check(
    settings: Settings,
    *,
    now: datetime | None = None,
    pending_cost: float = 0.0,
    pending_tokens: int = 0,
) -> BudgetState:
    """Décide si un run (ou la requête suivante) tient dans le budget."""
    cfg = getattr(settings, "budget", None)
    state = BudgetState(limits=_limits(settings))
    entries = _read(settings)
    moment = now or datetime.now(tz=timezone.utc)
    state.usage = usage(entries, moment)
    if cfg is None or not getattr(cfg, "enabled", True):
        return state

    usage_now = state.usage
    checks = (
        ("jour", "day", usage_now.day_cost + pending_cost, "daily_cost"),
        ("semaine", "week", usage_now.week_cost + pending_cost, "weekly_cost"),
        ("mois", "month", usage_now.month_cost + pending_cost, "monthly_cost"),
    )
    for label, _key, total, setting_name in checks:
        limit = float(getattr(cfg, setting_name, 0.0) or 0.0)
        if limit > 0 and total > limit:
            state.allowed = False
            state.reason = (
                f"budget {label} depasse : {total:.4f} USD pour un plafond de "
                f"{limit:.2f} USD"
            )
            return state
    token_checks = (
        ("jour", usage_now.day_tokens + pending_tokens, "daily_tokens"),
        ("mois", usage_now.month_tokens + pending_tokens, "monthly_tokens"),
    )
    for label, total, setting_name in token_checks:
        limit = int(getattr(cfg, setting_name, 0) or 0)
        if limit > 0 and total > limit:
            state.allowed = False
            state.reason = (
                f"budget {label} depasse : {total} tokens pour un plafond de {limit}"
            )
            return state
    state.reason = "budget disponible"
    state.projection_month_cost = _projection(state, moment)
    return state


def _projection(state: BudgetState, moment: datetime) -> float:
    """Projection de fin de mois au rythme observé (indicative)."""
    elapsed = max(1, moment.day)
    days = 28
    try:
        next_month = (moment.replace(day=28) + timedelta(days=4)).replace(day=1)
        days = (next_month - moment.replace(day=1)).days
    except ValueError:  # pragma: no cover - date impossible
        pass
    return round(state.usage.month_cost / elapsed * days, 4)


def format_state(state: BudgetState) -> str:
    """Rendu console, sans caractère hors cp1252."""
    usage_now = state.usage
    limits = state.limits
    lines = [
        f"Aujourd'hui : {usage_now.day_cost:.4f} USD, {usage_now.day_tokens} tokens, "
        f"{usage_now.runs_today} run(s)",
        f"Cette semaine : {usage_now.week_cost:.4f} USD",
        f"Ce mois : {usage_now.month_cost:.4f} USD, {usage_now.month_tokens} tokens, "
        f"{usage_now.runs_month} run(s)",
        f"Projection fin de mois : {state.projection_month_cost:.4f} USD",
    ]
    for key, label in (("day", "jour"), ("week", "semaine"), ("month", "mois")):
        limit = limits.get(key) or 0.0
        if limit:
            lines.append(f"Plafond {label} : {limit:.2f} USD")
    for key, label in (("day_tokens", "jour"), ("month_tokens", "mois")):
        limit = limits.get(key) or 0.0
        if limit:
            lines.append(f"Plafond {label} : {int(limit)} tokens")
    lines.append(f"Etat : {'ok' if state.allowed else state.reason}")
    if not any(limits.values()):
        lines.append("Aucun plafond configure : le registre sert de jauge.")
    return "\n".join(lines)


class BudgetRecorder:
    """Abonné au bus : enregistre tokens et coût à chaque `run_finished`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = ""

    def __call__(self, event: Any) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "run_started":
            self._model = str(payload.get("model") or "")
            return
        if event.type != "run_finished":
            return
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        record_run(
            self.settings,
            run_id=str(event.run_id),
            model=self._model or str(self.settings.llm.model),
            prompt_tokens=int(summary.get("prompt_tokens", 0) or 0),
            completion_tokens=int(summary.get("completion_tokens", 0) or 0),
        )


def build_budget_recorder(settings: Settings) -> BudgetRecorder | None:
    cfg = getattr(settings, "budget", None)
    if cfg is None or not getattr(cfg, "enabled", True):
        return None
    return BudgetRecorder(settings)


def main(argv: list[str] | None = None) -> int:
    del argv
    settings = Settings.load(strict=False)
    print(format_state(check(settings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
