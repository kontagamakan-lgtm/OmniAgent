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
    "RESERVATION_TTL_S",
    "BudgetState",
    "Usage",
    "build_budget_recorder",
    "check",
    "estimate_run",
    "format_state",
    "ledger_path",
    "record_run",
    "release_run",
    "reserve_run",
    "run_cost",
    "usage",
]

logger = logging.getLogger(f"{APP_ID}.budget")

LEDGER_NAME = "ledger.jsonl"
MAX_ENTRIES = 5000
#: Une réservation orpheline (processus tué) ne doit pas bloquer le budget
#: indéfiniment : au-delà, elle est ignorée puis purgée à la prochaine écriture.
RESERVATION_TTL_S = 6 * 3600


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
    month_average_run_tokens: int = 0
    #: Coût des runs en cours, réservé mais pas encore consolidé.
    reserved_cost: float = 0.0
    reserved_tokens: int = 0


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
                values.append(
                    limit
                    - float(getattr(self.usage, f"{name}_cost"))
                    - self.usage.reserved_cost
                )
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


def _write(settings: Settings, entries: list[dict[str, Any]]) -> None:
    """Réécrit le registre ; l'appelant tient déjà le verrou."""
    path = ledger_path(settings)
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries),
        encoding="utf-8",
    )
    temporary.replace(path)


def _is_reservation(entry: dict[str, Any]) -> bool:
    return str(entry.get("kind") or "") == "reservation"


def _active_reservations(
    entries: list[dict[str, Any]], moment: datetime
) -> list[dict[str, Any]]:
    """Réservations non expirées ; une entrée sans échéance est ignorée."""
    active: list[dict[str, Any]] = []
    for entry in entries:
        if not _is_reservation(entry):
            continue
        expires = _parse_at(entry.get("expires_at"))
        if expires is None or expires <= moment:
            continue
        active.append(entry)
    return active


def _prune(entries: list[dict[str, Any]], moment: datetime) -> tuple[list[dict[str, Any]], bool]:
    """Retire les réservations expirées ; dit si le registre a changé."""
    kept = [
        entry
        for entry in entries
        if not _is_reservation(entry)
        or (_parse_at(entry.get("expires_at")) or moment) > moment
    ]
    return kept, len(kept) != len(entries)


def _append(settings: Settings, record: dict[str, Any]) -> bool:
    """Ajoute un run terminé et consolide sa réservation dans le même verrou."""
    path = ledger_path(settings)
    run_id = str(record.get("run_id") or "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(".lock")):
            entries = _read(settings)
            # Le même run peut déjà avoir été enregistré par le bus : ne pas
            # compter deux fois son coût.
            if run_id and any(
                not _is_reservation(item) and str(item.get("run_id") or "") == run_id
                for item in entries
            ):
                return True
            if run_id:
                entries = [
                    item
                    for item in entries
                    if not (
                        _is_reservation(item)
                        and str(item.get("reservation_id") or "") == run_id
                    )
                ]
            entries.append(record)
            _write(settings, entries)
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
    month_tokens: list[int] = []
    for entry in entries:
        # Une réservation n'est pas une dépense : elle est comptée à part
        # (`BudgetState.usage.reserved_cost`) jusqu'à consolidation.
        if _is_reservation(entry):
            continue
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
            month_tokens.append(tokens)
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
        result.month_average_run_tokens = round(
            sum(month_tokens) / len(month_tokens)
        )
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


def _evaluate(
    settings: Settings,
    entries: list[dict[str, Any]],
    moment: datetime,
    *,
    pending_cost: float = 0.0,
    pending_tokens: int = 0,
    exclude_reservation: str = "",
) -> BudgetState:
    """Décision à partir d'un registre déjà lu (utilisable sous verrou)."""
    cfg = getattr(settings, "budget", None)
    state = BudgetState(limits=_limits(settings))
    state.usage = usage(entries, moment)
    reservations = [
        entry
        for entry in _active_reservations(entries, moment)
        if str(entry.get("reservation_id") or "") != exclude_reservation
    ]
    reserved_cost = sum(float(entry.get("cost") or 0.0) for entry in reservations)
    reserved_tokens = sum(int(entry.get("tokens") or 0) for entry in reservations)
    state.usage.reserved_cost = round(reserved_cost, 4)
    state.usage.reserved_tokens = reserved_tokens
    if cfg is None or not getattr(cfg, "enabled", True):
        return state

    usage_now = state.usage
    checks = (
        ("jour", "day", usage_now.day_cost + reserved_cost + pending_cost, "daily_cost"),
        ("semaine", "week", usage_now.week_cost + reserved_cost + pending_cost, "weekly_cost"),
        ("mois", "month", usage_now.month_cost + reserved_cost + pending_cost, "monthly_cost"),
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
        ("jour", usage_now.day_tokens + reserved_tokens + pending_tokens, "daily_tokens"),
        ("mois", usage_now.month_tokens + reserved_tokens + pending_tokens, "monthly_tokens"),
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


def check(
    settings: Settings,
    *,
    now: datetime | None = None,
    pending_cost: float = 0.0,
    pending_tokens: int = 0,
    exclude_reservation: str = "",
) -> BudgetState:
    """Décide si un run (ou la requête suivante) tient dans le budget.

    Les réservations des autres runs comptent dans les plafonds ; celle du run
    courant peut être exclue pour ne pas compter deux fois son estimation.
    """
    moment = now or datetime.now(tz=timezone.utc)
    return _evaluate(
        settings,
        _read(settings),
        moment,
        pending_cost=pending_cost,
        pending_tokens=pending_tokens,
        exclude_reservation=exclude_reservation,
    )


def estimate_run(
    settings: Settings, *, prompt_tokens: int = 0
) -> tuple[float, int]:
    """Estimation d'un run, tenant compte des runs multi-étapes passés.

    La base reste le prompt connu plus une sortie maximale, mais un run
    multi-étapes peut appeler le modèle plusieurs fois. La moyenne des runs
    terminés du mois sert donc de plancher, renforcée par
    `budget.estimate_multiplier`. Le coût réel remplace l'estimation à la fin.
    """
    prompt = max(0, int(prompt_tokens))
    completion = max(0, int(settings.llm.max_output_tokens))
    tokens = prompt + completion
    cost = run_cost(settings, prompt, completion)

    cfg = getattr(settings, "budget", None)
    multiplier = max(1.0, float(getattr(cfg, "estimate_multiplier", 1.0) or 1.0))
    # Le facteur explicite s'applique aussi au plancher sans historique :
    # l'utilisateur peut renforcer la marge avant le premier run multi-étapes.
    cost *= multiplier
    tokens = int(round(tokens * multiplier))
    history = usage(_read(settings))
    if history.runs_month:
        tokens = max(
            tokens,
            int(round(history.month_average_run_tokens * multiplier)),
        )
        cost = max(cost, round(history.month_average_run_cost * multiplier, 6))
    return cost, tokens


def reserve_run(
    settings: Settings,
    *,
    run_id: str,
    cost: float,
    tokens: int,
    now: datetime | None = None,
    ttl_s: int = RESERVATION_TTL_S,
) -> tuple[bool, str, str]:
    """Réserve atomiquement un coût estimé ; `(autorisé, raison, id)`.

    Lecture, contrôle et écriture se font sous le même verrou de fichier : deux
    processus ne peuvent pas passer tous les deux sur le même reste. Un registre
    inaccessible refuse (fail-closed), pour ne jamais dépenser à l'aveugle.
    """
    cfg = getattr(settings, "budget", None)
    if cfg is None or not getattr(cfg, "enabled", True):
        return True, "", ""
    moment = now or datetime.now(tz=timezone.utc)
    estimate = max(0.0, float(cost))
    estimated_tokens = max(0, int(tokens))
    identifier = str(run_id or "").strip()
    if not identifier:
        return False, "budget : run sans identifiant", ""
    record = {
        "kind": "reservation",
        "reservation_id": identifier,
        "run_id": identifier,
        "at": moment.isoformat(),
        "expires_at": (moment + timedelta(seconds=max(60, int(ttl_s)))).isoformat(),
        "cost": estimate,
        "tokens": estimated_tokens,
    }
    path = ledger_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(".lock")):
            entries = _read(settings)
            entries, changed = _prune(entries, moment)
            state = _evaluate(
                settings,
                entries,
                moment,
                pending_cost=estimate,
                pending_tokens=estimated_tokens,
            )
            if not state.allowed:
                if changed:
                    _write(settings, entries)
                return False, state.reason, ""
            entries.append(record)
            _write(settings, entries)
    except (OSError, LockTimeout):
        logger.warning("budget : reservation impossible", exc_info=True)
        return False, "budget indisponible : registre verrouille", ""
    return True, "", identifier


def release_run(
    settings: Settings, reservation_id: str, *, now: datetime | None = None
) -> bool:
    """Libère une réservation sans dépense (run arrêté avant tout appel)."""
    identifier = str(reservation_id or "").strip()
    if not identifier:
        return True
    path = ledger_path(settings)
    moment = now or datetime.now(tz=timezone.utc)
    try:
        with file_lock(path.with_suffix(".lock")):
            entries = _read(settings)
            before = len(entries)
            kept = [
                entry
                for entry in entries
                if not (
                    _is_reservation(entry)
                    and str(entry.get("reservation_id") or "") == identifier
                )
            ]
            kept, _changed = _prune(kept, moment)
            if len(kept) != before:
                _write(settings, kept)
    except (OSError, LockTimeout):
        logger.warning("budget : liberation impossible", exc_info=True)
        return False
    return True


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
    if usage_now.reserved_cost or usage_now.reserved_tokens:
        lines.append(
            "Reservations en cours : "
            f"{usage_now.reserved_cost:.4f} USD, {usage_now.reserved_tokens} tokens"
        )
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
