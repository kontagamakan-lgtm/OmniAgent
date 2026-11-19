"""Détection d'anomalies sur les métriques récentes.

Les métriques par run existent ; sans seuil, personne ne les regarde. Cette
fenêtre glissante (50 derniers runs par défaut) signale les dérives qui méritent
un coup d'œil : taux d'échec élevé, outil qui échoue en série, replis de
contexte fréquents, reprises LLM, runs anormalement longs ou coûteux.

Ce n'est pas une alerte de production : pas de notification, pas de blocage.
Les seuils sont volontairement simples et lisibles, et une fenêtre trop petite
(< 5 runs) n'alerte pas pour éviter le bruit des premiers essais.
"""

from __future__ import annotations

import statistics
from typing import Any

__all__ = ["ALERT_WINDOW", "detect_anomalies"]

ALERT_WINDOW = 50
MIN_RUNS = 5


def detect_anomalies(
    records: list[dict[str, Any]], *, window: int = ALERT_WINDOW
) -> list[str]:
    """Messages d'alerte, du plus grave au plus anodin."""
    recent = [record for record in records if isinstance(record, dict)][-max(1, window) :]
    if len(recent) < MIN_RUNS:
        return []
    alerts: list[tuple[int, str]] = []
    failed = sum(1 for record in recent if not record.get("ok"))
    rate = failed / len(recent)
    if rate >= 0.5:
        alerts.append((0, f"taux d'echec {rate:.0%} sur {len(recent)} runs"))

    tool_errors: dict[str, int] = {}
    for record in recent:
        errors = record.get("tool_errors")
        if isinstance(errors, dict):
            for name, count in errors.items():
                tool_errors[str(name)] = tool_errors.get(str(name), 0) + int(count or 0)
    for name, count in sorted(tool_errors.items(), key=lambda item: -item[1])[:2]:
        if count >= 3:
            alerts.append((1, f"outil {name} : {count} echecs"))

    trims = sum(int(record.get("trims", 0) or 0) for record in recent)
    if trims / len(recent) >= 3:
        alerts.append(
            (2, f"{trims} replis de contexte sur {len(recent)} runs (contexte trop court ?)")
        )

    retries = sum(int(record.get("retries", 0) or 0) for record in recent)
    if retries / len(recent) >= 2:
        alerts.append((2, f"{retries} reprises LLM sur {len(recent)} runs"))

    durations = [float(record.get("duration_s", 0) or 0) for record in recent]
    if len(durations) >= MIN_RUNS:
        median = statistics.median(durations)
        longest = max(durations)
        if median > 0 and longest > median * 5:
            alerts.append(
                (3, f"run de {longest:.0f} s contre une mediane de {median:.0f} s")
            )

    costs = [float(record.get("cost", 0) or 0) for record in recent]
    if costs and max(costs) >= 1.0:
        alerts.append((3, f"run a {max(costs):.2f} USD (seuil 1 USD)"))
    alerts.sort(key=lambda item: item[0])
    return [message for _, message in alerts]
