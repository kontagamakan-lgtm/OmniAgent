"""Bus d'événements thread-safe entre l'agent et l'interface.

L'agent tourne dans un thread de travail ; l'interface CustomTkinter ne peut
être touchée que depuis le thread principal. Le bus sert d'amortisseur :
`emit()` depuis n'importe quel thread, `poll()` (ou `wait()`) côté interface.

Deux canaux cohabitent :

* un **historique borné** (`events()`), consultable pour reconstruire l'état ;
* une **file d'attente** consommable une seule fois (`poll()`), pour l'affichage.

Un abonné qui lève une exception ne casse ni le bus ni les autres abonnés :
l'erreur est journalisée et ignorée.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from config import APP_ID

__all__ = [
    "Event",
    "EventBus",
    "EventType",
    "Subscriber",
]

logger = logging.getLogger(f"{APP_ID}.events")

Subscriber = Callable[["Event"], None]


class EventType:
    """Constantes de types d'événements (chaînes : lisibles dans les logs)."""

    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_RETRY = "llm_retry"
    STREAM_CHUNK = "stream_chunk"
    CONTEXT_TRIMMED = "context_trimmed"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    THOUGHT = "thought"
    FINAL = "final"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


@dataclass(slots=True)
class Event:
    """Un fait survenu pendant l'exécution."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    timestamp: float = field(default_factory=time.time)
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": self.payload,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
        }


class EventBus:
    """Bus minimaliste : historique borné + file consommable + abonnés."""

    def __init__(self, *, history_limit: int = 500, queue_limit: int = 5000) -> None:
        self._history: deque[Event] = deque(maxlen=max(1, history_limit))
        self._queue: deque[Event] = deque()
        self._queue_limit = max(1, queue_limit)
        self._subscribers: list[tuple[Subscriber, frozenset[str] | None, object]] = []
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0
        self._closed = False
        self.dropped = 0

    # -- émission -----------------------------------------------------------

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        run_id: str = "",
    ) -> Event:
        """Publie un événement depuis n'importe quel thread."""
        with self._cond:
            self._seq += 1
            event = Event(
                type=event_type,
                payload=dict(payload or {}),
                seq=self._seq,
                run_id=run_id,
            )
            self._history.append(event)
            if len(self._queue) >= self._queue_limit:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(event)
            targets = [
                handler
                for handler, types, _token in self._subscribers
                if types is None or event_type in types
            ]
            self._cond.notify_all()

        # Les abonnés sont appelés hors verrou : un abonné lent ne bloque pas
        # les producteurs, et un abonné fautif n'interrompt pas la boucle.
        for handler in targets:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - un abonné défaillant est isolé
                logger.exception("abonné d'événements en échec sur %s", event_type)
        return event

    # -- abonnements --------------------------------------------------------

    def subscribe(self, handler: Subscriber, *, types: Iterable[str] | None = None) -> Callable[[], None]:
        """Abonne un appelable ; retourne la fonction de désabonnement."""
        allowed = frozenset(types) if types is not None else None
        token = object()
        with self._lock:
            self._subscribers.append((handler, allowed, token))

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers = [
                    entry for entry in self._subscribers if entry[2] is not token
                ]

        return unsubscribe

    # -- consommation -------------------------------------------------------

    def poll(self, max_items: int = 200) -> list[Event]:
        """Retire et retourne jusqu'à `max_items` événements en attente."""
        with self._lock:
            count = min(len(self._queue), max(1, int(max_items)))
            return [self._queue.popleft() for _ in range(count)]

    def wait(self, timeout: float | None = 1.0, max_items: int = 200) -> list[Event]:
        """Comme `poll`, mais attend un événement jusqu'à `timeout` secondes."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._cond:
            while not self._queue and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._cond.wait(remaining)
            count = min(len(self._queue), max(1, int(max_items)))
            return [self._queue.popleft() for _ in range(count)]

    def events(self, *, event_type: str = "", limit: int = 0) -> list[Event]:
        """Historique (le plus ancien d'abord), éventuellement filtré."""
        with self._lock:
            items = [
                event for event in self._history if not event_type or event.type == event_type
            ]
        if limit and len(items) > limit:
            return items[-limit:]
        return items

    def history(self, run_id: str | None = None) -> list[Event]:
        """Historique complet, éventuellement restreint à une exécution."""
        items = self.events()
        return items if run_id is None else [e for e in items if e.run_id == run_id]

    def close(self) -> None:
        """Réveille les consommateurs bloqués et vide les abonnés."""
        with self._cond:
            self._closed = True
            self._subscribers.clear()
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)
