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


# One indivisible admission: sweep what expired, sum what is live, decide, and
# record — server-side, so two replicas cannot both read the same total and both
# slip under the cap. Splitting this into a read and a write in Python would
# reintroduce exactly the race the single-process gate avoided by not awaiting
# between them.
#
# Field format is "amount:expires_at". A hash rather than a key per reservation
# because the sum has to be taken over a set the script can enumerate; the outer
# EXPIRE is the safety net for a team that stops sending traffic entirely.
_RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local spent = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local amount = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local reservation_id = ARGV[6]

local reserved = 0
local expired = {}
local entries = redis.call('HGETALL', KEYS[1])
for i = 1, #entries, 2 do
  local field = entries[i]
  local value = entries[i + 1]
  local separator = string.find(value, ':')
  local held = tonumber(string.sub(value, 1, separator - 1))
  local expires_at = tonumber(string.sub(value, separator + 1))
  if expires_at <= now then
    table.insert(expired, field)
  else
    reserved = reserved + held
  end
end
if #expired > 0 then
  redis.call('HDEL', KEYS[1], unpack(expired))
end
if spent + reserved >= limit then
  return {0, tostring(reserved)}
end
redis.call('HSET', KEYS[1], reservation_id, tostring(amount) .. ':' .. tostring(now + ttl))
redis.call('EXPIRE', KEYS[1], ttl * 2)
return {1, tostring(reserved)}
"""


class RedisBudgetReservationStore:
    """In-flight spend shared across replicas.

    The decision runs as a Lua script so the read-decide-write is one atomic
    step on the server. `now` is passed in rather than read from Redis: the
    caller's clock is the one the rest of the budget window already uses, and it
    keeps the store testable against a controlled clock."""

    def __init__(self, client: object, *, clock: Callable[[], float] = time.time) -> None:
        # redis.asyncio.Redis; typed as object to avoid a hard import at module load.
        self._redis = client
        self._clock = clock
        self._script = client.register_script(_RESERVE_SCRIPT)  # type: ignore[attr-defined]

    @staticmethod
    def _key(team_id: UUID) -> str:
        return f"budget:res:{team_id}"

    async def try_reserve(
        self,
        team_id: UUID,
        amount: float,
        *,
        spent: float,
        limit: float,
        ttl_s: int,
    ) -> ReservationOutcome:
        reservation_id = uuid4()
        admitted, reserved = await self._script(
            keys=[self._key(team_id)],
            args=[self._clock(), spent, limit, amount, ttl_s, str(reservation_id)],
        )
        reserved_total = float(reserved)
        if not int(admitted):
            return ReservationOutcome(reservation=None, reserved=reserved_total)
        return ReservationOutcome(
            reservation=Reservation(id=reservation_id, team_id=team_id, amount=amount),
            reserved=reserved_total,
        )

    async def release(self, reservation: Reservation) -> None:
        # HDEL by id: releasing twice, or releasing one the sweep already
        # removed, deletes nothing and changes nothing.
        await self._redis.hdel(  # type: ignore[attr-defined]
            self._key(reservation.team_id), str(reservation.id)
        )


def build_budget_reservation_store(settings: Settings) -> BudgetReservationStore:
    """Redis-backed when REDIS_URL is set (shared across replicas), else
    in-memory. Production cannot reach the in-memory branch: `Settings` refuses
    to start without `REDIS_URL`."""
    if settings.redis_url:
        from redis.asyncio import Redis

        return RedisBudgetReservationStore(Redis.from_url(settings.redis_url))
    return InMemoryBudgetReservationStore()
