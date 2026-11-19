"""Contrôleur d'exécution : la boucle ReAct dans un thread, pour l'interface.

Aucune dépendance graphique ici. L'interface appelle `start()`, puis vide la
file d'événements avec `drain()` depuis son propre fil (via `after()`), ce qui
évite tout accès à Tk depuis le thread de travail. Les tests font la même
chose sans écran.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from config import APP_ID
from core.agent_core import AgentCore, AgentReply
from core.events import Event, EventType
from core.trace import RunTrace, build_trace

__all__ = ["AgentController", "AgentRun"]

logger = logging.getLogger(f"{APP_ID}.ui")


@dataclass(slots=True)
class AgentRun:
    """État d'une exécution demandée par l'interface."""

    task: str
    reply: AgentReply | None = None
    error: str = ""
    duration_s: float = 0.0
    finished: bool = False
    events: list[Event] = field(default_factory=list)
    #: Texte reçu en flux, concaténé dans l'ordre d'arrivée.
    streamed: str = ""

    @property
    def ok(self) -> bool:
        return self.reply is not None and self.reply.ok


class AgentController:
    """Pilote un `AgentCore` de façon asynchrone et sûre pour l'UI."""

    def __init__(self, agent: AgentCore, *, drain_limit: int = 200) -> None:
        self.agent = agent
        self.drain_limit = max(1, int(drain_limit))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._finished = threading.Event()
        self._current: AgentRun | None = None
        self._started = time.monotonic()
        # Le texte en flux s'accumule à l'émission, pas au vidage : un test ou
        # un consommateur lent doit pouvoir relire l'intégralité sans dépendre
        # de ce qu'il a déjà retiré de la file.
        self._unsubscribe = self.agent.bus.subscribe(
            self._collect, types=(EventType.STREAM_CHUNK,)
        )
        # Une trace par exécution, consultable après coup depuis l'interface.
        # `build_trace` respecte `trace.enabled` : désactivée, elle vaut None et
        # rien n'est écrit sur disque.
        self.trace: RunTrace | None = build_trace(self.agent.settings)
        self._unsubscribe_trace = (
            self.agent.bus.subscribe(self.trace) if self.trace is not None else (lambda: None)
        )

    # -- état ---------------------------------------------------------------

    @property
    def busy(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def current(self) -> AgentRun | None:
        return self._current

    @property
    def tracing(self) -> bool:
        """Vrai si les exécutions sont actuellement tracées sur disque."""
        return self.trace is not None

    def set_tracing(self, enabled: bool) -> None:
        """Active ou coupe la trace, sans recréer le contrôleur.

        L'utilisateur peut ne pas vouloir laisser le contenu d'une session sur
        disque ; le basculer ici évite de redémarrer l'application. Une trace
        déjà ouverte est refermée, ce qui retire aussi son marqueur d'activité.
        """
        if enabled and self.trace is None:
            self.trace = build_trace(self.agent.settings)
            if self.trace is not None:
                self._unsubscribe_trace = self.agent.bus.subscribe(self.trace)
        elif not enabled and self.trace is not None:
            self._unsubscribe_trace()
            self._unsubscribe_trace = lambda: None
            self.trace.close()
            self.trace = None

    @property
    def reply(self) -> AgentReply | None:
        return self._current.reply if self._current else None

    # -- pilotage -----------------------------------------------------------

    def start(self, task: str) -> bool:
        """Démarre une tâche ; refuse si une exécution est déjà en cours."""
        prompt = str(task or "").strip()
        if not prompt:
            return False
        with self._lock:
            if self.busy:
                return False
            run = AgentRun(task=prompt)
            self._current = run
            self._finished.clear()
            self._started = time.monotonic()
            self.agent.reset_cancel()
            self._thread = threading.Thread(
                target=self._run, args=(run,), name="omniagent-run", daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        """Demande l'arrêt coopératif de l'exécution en cours."""
        self.agent.cancel()

    def wait(self, timeout: float | None = None) -> AgentReply | None:
        """Attend la fin de l'exécution et retourne sa réponse (ou None).

        Le drapeau est posé avant la sortie du thread : sans ce `join`, un
        appelant qui teste `busy` juste après `wait()` peut encore le voir vrai
        sur une machine lente.
        """
        if self._finished.wait(timeout) and self._thread is not None:
            # Le drapeau est posé avant le scellement d'audit : laisser au
            # thread le temps de finir même sur une machine chargée.
            self._thread.join(5.0)
        return self.reply

    def drain(self, max_items: int | None = None) -> list[Event]:
        """Vide la file d'événements ; à appeler depuis le fil de l'interface."""
        events = self.agent.bus.poll(max_items or self.drain_limit)
        run = self._current
        if run is not None and events:
            run.events.extend(events)
        return events

    def _collect(self, event: Event) -> None:
        """Accumule un fragment de flux dans l'exécution courante."""
        run = self._current
        if run is None or not isinstance(event.payload, dict):
            return
        run.streamed += str(event.payload.get("text", ""))

    def close(self) -> None:
        """Se désabonne du bus (à appeler lorsque la fenêtre se ferme)."""
        self._unsubscribe()
        self._unsubscribe_trace()
        if self.trace is not None:
            self.trace.close()

    # -- exécution ----------------------------------------------------------

    def _run(self, run: AgentRun) -> None:
        try:
            run.reply = self.agent.run(run.task)
        except Exception as exc:  # noqa: BLE001 - l'interface doit rester vivante
            logger.exception("exécution de l'agent interrompue")
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.duration_s = round(time.monotonic() - self._started, 3)
            run.finished = True
            self._finished.set()
            # Scelle la trace terminée : une modification ulérieure sera
            # détectable par `--verify-audit`.
            trace = self.trace
            if trace is not None and trace.path is not None:
                try:
                    from core.audit import seal_trace

                    seal_trace(self.agent.settings, trace.path)
                except Exception:  # noqa: BLE001 - l'audit ne casse pas le run
                    logger.debug("scellement d'audit impossible", exc_info=True)

    def __repr__(self) -> str:  # pragma: no cover - confort
        return f"<AgentController busy={self.busy}>"


def last_text(events: list[Event], default: str = "") -> str:
    """Dernier texte final trouvé dans une liste d'événements."""
    for event in reversed(events):
        payload: Any = event.payload
        if isinstance(payload, dict) and event.type == "final" and payload.get("text"):
            return str(payload["text"])
    return default
