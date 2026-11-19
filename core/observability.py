"""Contexte d'exécution partagé par le journal et les rapports.

Une trace porte un `run_id`, le journal n'en portait aucun : joindre une ligne
de journal à un incident demandait de comparer des horodatages. Le contexte est
un `ContextVar`, donc propre au thread de l'exécution — le thread d'interface
reste à `-`.

    token = set_run_id("abc123")
    ...
    reset_run_id(token)
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

__all__ = ["RunIdFilter", "current_run_id", "reset_run_id", "run_label", "set_run_id"]

_EMPTY = "-"

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "omniagent_run_id", default=_EMPTY
)


def set_run_id(value: str) -> contextvars.Token[str]:
    """Pose l'identifiant d'exécution courant, retourne le jeton de retour."""
    return _run_id.set(str(value or _EMPTY))


def reset_run_id(token: contextvars.Token[str]) -> None:
    """Restaure le contexte précédent (fin d'exécution)."""
    try:
        _run_id.reset(token)
    except ValueError:  # noqa: BLE001 - jeton d'un autre contexte : sans effet
        pass


def current_run_id() -> str:
    return _run_id.get()


def run_label() -> dict[str, Any]:
    """Champs à recopier dans un rapport produit par un outil."""
    run_id = current_run_id()
    return {"run": run_id} if run_id and run_id != _EMPTY else {}


class RunIdFilter(logging.Filter):
    """Ajoute `record.run_id` pour le format `%(run_id)s`."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "run_id", None):
            record.run_id = current_run_id()
        return True
