"""Minuteurs `after` qui ne s'empilent pas.

Un `after` relancé sans annuler son prédécesseur laisse deux minuteurs vivants :
un rafraîchissement déclenché à la main double la cadence d'une boucle déjà
planifiée, et une animation relancée se bat contre la précédente. `SingleTimer`
garde l'identifiant du minuteur en cours, l'annule avant d'en poser un autre, et
l'oublie quand il a tiré.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["SingleTimer"]


class SingleTimer:
    """Un seul minuteur vivant par boucle, quel que soit le nombre de relances."""

    def __init__(
        self,
        schedule: Callable[[int, Callable[[], None]], Any],
        cancel: Callable[[Any], None],
    ) -> None:
        self._schedule = schedule
        self._cancel = cancel
        self._pending: Any = None

    @property
    def pending(self) -> bool:
        """Vrai tant qu'un minuteur attend de tirer."""
        return self._pending is not None

    def start(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """Programme `callback` en remplaçant le minuteur qui attendait."""
        self.cancel()

        def fire() -> None:
            self._pending = None
            callback()

        self._pending = self._schedule(delay_ms, fire)

    def cancel(self) -> None:
        """Annule le minuteur en attente, s'il y en a un."""
        if self._pending is not None:
            self._cancel(self._pending)
            self._pending = None
