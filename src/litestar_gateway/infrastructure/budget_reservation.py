"""Budget reservation stores — in-memory for local development, Redis for
anything with more than one replica.

Mirrors `infrastructure/rate_limiter.py` and `infrastructure/circuit_breaker.py`:
one port, two adapters, selected by `REDIS_URL`. The two are held to a single
conformance suite rather than a test file each — the divergence in ISSUE-029
came from exactly that split.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID, uuid4

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import (
    BudgetReservationStore,
    Reservation,
    ReservationOutcome,
)


class InMemoryBudgetReservationStore:
    """Process-local in-flight spend. Correct for a single replica — which is
    what local development is — and the reason production refuses to start
    without Redis: with N replicas this bounds the overshoot N times, once per
    process, instead of once."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # team -> reservation id -> (amount, expires_at)
        self._by_team: dict[UUID, dict[UUID, tuple[float, float]]] = {}

    def _live_total(self, team_id: UUID) -> float:
        """Sum the team's unexpired reservations, dropping the rest on the way
        through — the same opportunistic sweep the Redis script performs, so a
        replica that died mid-request cannot hold headroom forever."""
        held = self._by_team.get(team_id)
        if not held:
            return 0.0
        now = self._clock()
        live = {res_id: v for res_id, v in held.items() if v[1] > now}
        if live:
            self._by_team[team_id] = live
        else:
            self._by_team.pop(team_id, None)
        return sum(amount for amount, _ in live.values())

    async def try_reserve(
        self,
        team_id: UUID,
        amount: float,
        *,
        spent: float,
        limit: float,
        ttl_s: int,
    ) -> ReservationOutcome:
        # No await anywhere between the read and the write: concurrent gates in
        # one event loop interleave only at checkpoints, so two requests cannot
        # both see the same total and both slip through.
        reserved = self._live_total(team_id)
        if spent + reserved >= limit:
            return ReservationOutcome(reservation=None, reserved=reserved)
        reservation = Reservation(id=uuid4(), team_id=team_id, amount=amount)
        self._by_team.setdefault(team_id, {})[reservation.id] = (
            amount,
            self._clock() + ttl_s,
        )
        return ReservationOutcome(reservation=reservation, reserved=reserved)

    async def release(self, reservation: Reservation) -> None:
        held = self._by_team.get(reservation.team_id)
        if held is None:
            return
        held.pop(reservation.id, None)
        if not held:
            self._by_team.pop(reservation.team_id, None)


def build_budget_reservation_store(settings: Settings) -> BudgetReservationStore:
    """Redis-backed when REDIS_URL is set (shared across replicas), else
    in-memory. Production cannot reach the in-memory branch: `Settings` refuses
    to start without `REDIS_URL`."""
    return InMemoryBudgetReservationStore()
