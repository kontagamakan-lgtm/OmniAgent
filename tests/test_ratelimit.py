"""Le limiteur de cadence : rafale permise, puis une requête toutes les `60/n` s."""

from __future__ import annotations

from core.ratelimit import RequestBudget


class _Horloge:
    """Horloge et sommeil factices : les tests n'attendent pas réellement."""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


def test_a_null_cap_never_waits() -> None:
    horloge = _Horloge()
    budget = RequestBudget(0, clock=horloge.clock, sleep=horloge.sleep)

    for _ in range(100):
        assert budget.acquire() == 0.0

    assert budget.enabled is False
    assert horloge.delays == []


def test_the_burst_is_free_then_the_rate_kicks_in() -> None:
    horloge = _Horloge()
    budget = RequestBudget(6, clock=horloge.clock, sleep=horloge.sleep)

    # 6 appels par minute : six jetons disponibles d'emblée.
    assert [budget.acquire() for _ in range(6)] == [0.0] * 6
    assert horloge.delays == []

    # Le septième doit attendre un jeton complet : 60 / 6 = 10 secondes.
    assert budget.acquire() == 10.0
    assert horloge.delays == [10.0]
    assert budget.waits == 1


def test_tokens_refill_with_time() -> None:
    horloge = _Horloge()
    budget = RequestBudget(60, clock=horloge.clock, sleep=horloge.sleep)

    for _ in range(60):
        budget.acquire()
    assert budget.acquire() == 1.0

    # Une minute plus tard, le seau est de nouveau plein : plus aucune attente.
    horloge.now += 60.0
    assert [budget.acquire() for _ in range(60)] == [0.0] * 60


def test_describe_exposes_the_state() -> None:
    budget = RequestBudget(30, clock=_Horloge().clock, sleep=lambda _delay: None)

    assert budget.describe() == {"per_minute": 30, "enabled": True, "waits": 0}
    assert RequestBudget(-5).describe()["enabled"] is False
