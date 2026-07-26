"""Circuit-breaker adapters — closed/open/half-open state machine per key.

In-memory by default (per-process); Redis-backed when a REDIS_URL is
configured, so the breaker state (and its single half-open trial) is shared
across replicas. Both implement the `CircuitBreaker` port. Mirrors
`infrastructure/rate_limiter.py`'s in-memory-vs-Redis split.

State machine: **closed** admits every call; each failure increments a
counter and a success resets it, so only *consecutive* failures count.
Reaching `failure_threshold` consecutive failures trips the breaker to
**open**, where `allow()` is False until `cooldown_seconds` have elapsed.
Once the cooldown elapses, the breaker moves to **half-open** and grants
exactly one trial (the same `allow()` call that observes the elapsed
cooldown both transitions the state and grants the trial, so no other
concurrent caller also gets one). The trial's outcome decides: success closes
the breaker (counter reset); failure re-opens it with a fresh cooldown from
now.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import CircuitBreaker

_Status = Literal["closed", "open", "half_open"]


@dataclass(slots=True)
class _State:
    status: _Status = "closed"
    failure_count: int = 0
    opened_at: float | None = None


class InMemoryCircuitBreaker:
    """Process-local breaker. Correct for a single replica; for multi-replica
    deployments configure Redis so tripped state is shared fleet-wide."""

    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._states: dict[str, _State] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        async with self._lock:
            state = self._states.setdefault(key, _State())
            if state.status == "closed":
                return True
            if state.status == "half_open":
                # A trial is already in flight; no second concurrent trial.
                return False
            # status == "open"
            assert state.opened_at is not None
            if self._clock() >= state.opened_at + self._cooldown_seconds:
                state.status = "half_open"
                return True
            return False

    async def record_failure(self, key: str) -> None:
        async with self._lock:
            state = self._states.setdefault(key, _State())
            if state.status == "half_open":
                # The trial failed: re-open with a fresh cooldown from now.
                state.status = "open"
                state.opened_at = self._clock()
                return
            if state.status == "open":
                return
            state.failure_count += 1
            if state.failure_count >= self._failure_threshold:
                state.status = "open"
                state.opened_at = self._clock()

    async def record_success(self, key: str) -> None:
        async with self._lock:
            state = self._states.setdefault(key, _State())
            if state.status == "open":
                # Only the half-open trial's outcome closes an open breaker.
                return
            state.status = "closed"
            state.failure_count = 0
            state.opened_at = None


class RedisCircuitBreaker:
    """Shared breaker across replicas. Failure count lives on a plain INCR'd
    key (reset on success via DELETE); the half-open trial is claimed with
    `SET key value NX EX cooldown_seconds` on a separate marker key -- only
    one replica's `SET` can win, so only one caller gets the trial."""

    def __init__(
        self,
        client: object,
        failure_threshold: int,
        cooldown_seconds: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # redis.asyncio.Redis; typed as object to avoid a hard import at module load.
        self._redis = client
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock

    def _failures_key(self, key: str) -> str:
        return f"cb:failures:{key}"

    def _opened_key(self, key: str) -> str:
        return f"cb:opened:{key}"

    def _trial_key(self, key: str) -> str:
        return f"cb:trial:{key}"

    async def allow(self, key: str) -> bool:
        opened_raw = await self._redis.get(self._opened_key(key))  # type: ignore[attr-defined]
        if opened_raw is None:
            return True  # closed: no open marker recorded
        opened_at = float(opened_raw)
        if self._clock() < opened_at + self._cooldown_seconds:
            return False  # still cooling down
        # Cooldown elapsed: claim the single half-open trial atomically.
        claimed = await self._redis.set(  # type: ignore[attr-defined]
            self._trial_key(key), "1", nx=True, ex=self._cooldown_seconds
        )
        return bool(claimed)

    async def record_failure(self, key: str) -> None:
        trial_raw = await self._redis.get(self._trial_key(key))  # type: ignore[attr-defined]
        if trial_raw is not None:
            # The half-open trial failed: re-open with a fresh cooldown, and
            # release the trial marker so the next elapsed cooldown can claim
            # a new one.
            await self._redis.delete(self._trial_key(key))  # type: ignore[attr-defined]
            await self._redis.set(  # type: ignore[attr-defined]
                self._opened_key(key), str(self._clock()), ex=self._cooldown_seconds
            )
            return
        opened_raw = await self._redis.get(self._opened_key(key))  # type: ignore[attr-defined]
        if opened_raw is not None:
            return  # already open; nothing new to count
        count = await self._redis.incr(self._failures_key(key))  # type: ignore[attr-defined]
        if count == 1:
            await self._redis.expire(  # type: ignore[attr-defined]
                self._failures_key(key), self._cooldown_seconds
            )
        if count >= self._failure_threshold:
            await self._redis.delete(self._failures_key(key))  # type: ignore[attr-defined]
            await self._redis.set(  # type: ignore[attr-defined]
                self._opened_key(key), str(self._clock()), ex=self._cooldown_seconds
            )

    async def record_success(self, key: str) -> None:
        opened_raw = await self._redis.get(self._opened_key(key))  # type: ignore[attr-defined]
        trial_raw = await self._redis.get(self._trial_key(key))  # type: ignore[attr-defined]
        if opened_raw is not None and trial_raw is None:
            # Open, no trial in flight: only the half-open trial's outcome
            # closes an open breaker.
            return
        await self._redis.delete(  # type: ignore[attr-defined]
            self._failures_key(key), self._opened_key(key), self._trial_key(key)
        )


def build_circuit_breaker(settings: Settings) -> CircuitBreaker:
    """Redis-backed when REDIS_URL is set (shared across replicas), else in-memory."""
    if settings.redis_url:
        from redis.asyncio import Redis

        return RedisCircuitBreaker(
            Redis.from_url(settings.redis_url),
            settings.circuit_breaker_failure_threshold,
            settings.circuit_breaker_cooldown_seconds,
        )
    return InMemoryCircuitBreaker(
        settings.circuit_breaker_failure_threshold,
        settings.circuit_breaker_cooldown_seconds,
    )
