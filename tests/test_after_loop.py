"""Le minuteur unique de l'interface, testé sans Tk.

Un `after` relancé sans annulation laisse deux boucles vivantes : le test le
prouve sur un ordonnanceur factice, sans fenêtre ni affichage.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.after_loop import SingleTimer  # noqa: E402


class _Scheduler:
    """Ordonnanceur factice : garde les rappels par identifiant."""

    def __init__(self) -> None:
        self.pending: dict[str, object] = {}
        self.cancelled: list[str] = []
        self._count = 0

    def schedule(self, delay_ms: int, callback) -> str:
        self._count += 1
        ident = f"t{self._count}"
        self.pending[ident] = (delay_ms, callback)
        return ident

    def cancel(self, ident: str) -> None:
        self.cancelled.append(ident)
        self.pending.pop(ident, None)

    def fire(self, ident: str) -> None:
        _delay, callback = self.pending.pop(ident)
        callback()


def _timer() -> tuple[SingleTimer, _Scheduler]:
    scheduler = _Scheduler()
    return SingleTimer(scheduler.schedule, scheduler.cancel), scheduler


def test_restarting_cancels_the_previous_timer() -> None:
    """Une boucle relancée à la main ne double pas sa cadence."""
    timer, scheduler = _timer()
    calls: list[str] = []

    timer.start(2000, lambda: calls.append("premier"))
    first = next(iter(scheduler.pending))
    timer.start(2000, lambda: calls.append("second"))

    assert scheduler.cancelled == [first]
    assert len(scheduler.pending) == 1, "un seul minuteur doit rester vivant"

    scheduler.fire(next(iter(scheduler.pending)))
    assert calls == ["second"]


def test_a_fired_timer_is_forgotten() -> None:
    """Après avoir tiré, le minuteur ne se croit plus en attente."""
    timer, scheduler = _timer()
    calls: list[str] = []

    timer.start(50, lambda: calls.append("tour"))
    assert timer.pending
    scheduler.fire(next(iter(scheduler.pending)))

    assert calls == ["tour"]
    assert not timer.pending
    timer.start(50, lambda: calls.append("suivant"))
    assert scheduler.cancelled == [], "rien à annuler après un tir"


def test_cancel_before_firing_prevents_the_callback() -> None:
    """Fermer une fenêtre annule sa boucle au lieu de la laisser tirer."""
    timer, scheduler = _timer()
    calls: list[str] = []

    timer.start(2000, lambda: calls.append("trop tard"))
    timer.cancel()

    assert not timer.pending
    assert scheduler.pending == {}
    assert calls == []
