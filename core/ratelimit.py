"""Plafond du nombre d'appels au fournisseur par minute.

`core.budget` plafonne ce que coûtent les appels, pas leur **fréquence** : un
run qui boucle sur des réponses courtes peut envoyer des dizaines de requêtes
par minute sans jamais approcher un plafond de dépense, et se faire limiter par
le fournisseur (HTTP 429) au milieu d'une tâche. Ce module borne la cadence
avant l'envoi, avec un seau à jetons : la rafale initiale est permise (une
requête par appel, jusqu'à `per_minute`), puis la cadence se stabilise à
`per_minute / 60` requêtes par seconde.

L'horloge et l'attente sont injectables : les tests n'attendent pas réellement.
Un plafond `<= 0` désactive le seau — un limiteur inactif ne dort jamais.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ["RequestBudget"]


class RequestBudget:
    """Seau à jetons : au plus `per_minute` appels par minute, rafale incluse."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        self.per_minute = max(0, int(per_minute or 0))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(self.per_minute)
        self._updated = clock()
        self.waits = 0

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def _refill(self) -> None:
        now = self._clock()
        rate = self.per_minute / 60.0
        if now > self._updated:
            self._tokens = min(
                float(self.per_minute), self._tokens + (now - self._updated) * rate
            )
            self._updated = now

    def acquire(self) -> float:
        """Prend un jeton, en attendant si nécessaire. Retourne l'attente subie."""
        if not self.enabled:
            return 0.0
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            missing = 1.0 - self._tokens
            delay = missing / (self.per_minute / 60.0)
            # Le seau est remis à zéro comme si le temps avait passé : sans cela
            # deux threads qui attendent en parallèle dormiraient deux fois.
            self._tokens = 1.0
            self._updated = self._clock() + delay
            self.waits += 1
        if delay > 0:
            self._sleep(delay)
        return delay

    def describe(self) -> dict[str, Any]:
        """État lisible par l'interface et les journaux."""
        return {
            "per_minute": self.per_minute,
            "enabled": self.enabled,
            "waits": self.waits,
        }
