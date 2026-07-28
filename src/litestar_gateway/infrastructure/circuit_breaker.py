"""Circuit-breaker adapters — closed/open/half-open state machine per key.

In-memory by default (per-process); Redis-backed when a REDIS_URL is
configured, so the breaker state (and its single half-open trial) is shared
across replicas. Both implement the `CircuitBreaker` port. Mirrors
`infrastructure/rate_limiter.py`'s in-memory-vs-Redis split.

State machine: **closed** admits every call; each failure increments a
counter and a success resets it, so only *consecutive* failures count.

An outcome only resolves the half-open trial when it carries that trial's own
token (ISSUE-033): a request admitted before the breaker opened can finish long
after another attempt took the trial, and nothing in its result says the
provider recovered. Tokenless outcomes still count towards tripping a closed
breaker and still reset its counter — they simply cannot decide a transition
they did not earn.
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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import BreakerLease, CircuitBreaker

_Status = Literal["closed", "open", "half_open"]


@dataclass(slots=True)
class _State:
    status: _Status = "closed"
    failure_count: int = 0
    opened_at: float | None = None
    # Identifies the in-flight half-open trial, so only its own outcome
    # resolves it (ISSUE-033).
    trial_token: str | None = None


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

    async def allow(self, key: str) -> BreakerLease:
        async with self._lock:
            state = self._states.setdefault(key, _State())
            if state.status == "closed":
                return BreakerLease(allowed=True)
            if state.status == "half_open":
                # A trial is already in flight; no second concurrent trial.
                return BreakerLease(allowed=False)
            # status == "open"
            assert state.opened_at is not None
            if self._clock() >= state.opened_at + self._cooldown_seconds:
                state.status = "half_open"
                state.trial_token = uuid.uuid4().hex
                return BreakerLease(allowed=True, trial_token=state.trial_token)
            return BreakerLease(allowed=False)

    async def record_failure(self, key: str, trial_token: str | None = None) -> None:
        async with self._lock:
            state = self._states.setdefault(key, _State())
            if state.status == "half_open":
                if trial_token is None or trial_token != state.trial_token:
                    return  # stale outcome: not this trial's to decide
                # The trial failed: re-open with a fresh cooldown from now.
                state.status = "open"
                state.opened_at = self._clock()
                state.trial_token = None
                return
            if state.status == "open":
                return
            state.failure_count += 1
            if state.failure_count >= self._failure_threshold:
                state.status = "open"
                state.opened_at = self._clock()

    async def record_success(self, key: str, trial_token: str | None = None) -> None:
        async with self._lock:
            state = self._states.setdefault(key, _State())
            if state.status == "half_open" and (
                trial_token is None or trial_token != state.trial_token
            ):
                return  # stale outcome: the trial holder still decides
            if state.status == "open":
                # Only the half-open trial's outcome closes an open breaker.
                return
            state.status = "closed"
            state.failure_count = 0
            state.opened_at = None
            state.trial_token = None


class RedisCircuitBreaker:
    """Shared breaker across replicas. Failure count lives on a plain INCR'd
    key (reset on success via DELETE); the half-open trial is claimed with
    `SET key token NX EX cooldown_seconds` on a separate marker key -- only one
    replica's `SET` can win, so only one caller gets the trial, and the stored
    value is that trial's token so an outcome can prove it owns it."""

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

    def _open_marker_ttl(self) -> int:
        """How long the `opened` marker lives.

        It must OUTLIVE the cooldown (ISSUE-029): the marker is what tells
        `allow()` the breaker is open, and the half-open branch — the one that
        claims the single trial — is only reached while it exists. With a TTL
        equal to the cooldown the marker vanished at the exact moment the
        breaker should have moved to half-open, so `allow()` read "closed" and
        admitted every concurrent caller at once, stampeding a provider that
        had just failed.

        One extra cooldown of retention is enough for the transition to be
        observed by whatever traffic arrives. Beyond that the key is allowed to
        expire: with no traffic at all there is no stampede to protect against,
        and a stale marker must never wedge a provider that has recovered."""
        return max(1, self._cooldown_seconds * 2)

    @staticmethod
    def _owns_trial(trial_raw: object, trial_token: str | None) -> bool:
        """Whether an outcome carries the token of the trial currently in
        flight. Redis may hand the value back as bytes."""
        if trial_token is None:
            return False
        stored = trial_raw.decode() if isinstance(trial_raw, bytes) else str(trial_raw)
        return stored == trial_token

    def _failures_key(self, key: str) -> str:
        return f"cb:failures:{key}"

    def _opened_key(self, key: str) -> str:
        return f"cb:opened:{key}"

    def _trial_key(self, key: str) -> str:
        return f"cb:trial:{key}"

    async def allow(self, key: str) -> BreakerLease:
        opened_raw = await self._redis.get(self._opened_key(key))  # type: ignore[attr-defined]
        if opened_raw is None:
            return BreakerLease(allowed=True)  # closed: no open marker recorded
        opened_at = float(opened_raw)
        if self._clock() < opened_at + self._cooldown_seconds:
            return BreakerLease(allowed=False)  # still cooling down
        # Cooldown elapsed: claim the single half-open trial. `SET NX` is the
        # atomic part — concurrent callers all attempt it and exactly one wins,
        # so no Lua script is needed for the grant itself; the marker's TTL
        # above is what guarantees they reach this branch at all.
        token = uuid.uuid4().hex
        claimed = await self._redis.set(  # type: ignore[attr-defined]
            self._trial_key(key), token, nx=True, ex=self._cooldown_seconds
        )
        if not claimed:
            return BreakerLease(allowed=False)
        return BreakerLease(allowed=True, trial_token=token)

    async def record_failure(self, key: str, trial_token: str | None = None) -> None:
        trial_raw = await self._redis.get(self._trial_key(key))  # type: ignore[attr-defined]
        if trial_raw is not None:
            if not self._owns_trial(trial_raw, trial_token):
                return  # stale outcome: not this trial's to decide
            # The half-open trial failed: re-open with a fresh cooldown, and
            # release the trial marker so the next elapsed cooldown can claim
            # a new one.
            await self._redis.delete(self._trial_key(key))  # type: ignore[attr-defined]
            await self._redis.set(  # type: ignore[attr-defined]
                self._opened_key(key), str(self._clock()), ex=self._open_marker_ttl()
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
                self._opened_key(key), str(self._clock()), ex=self._open_marker_ttl()
            )

    async def record_success(self, key: str, trial_token: str | None = None) -> None:
        opened_raw = await self._redis.get(self._opened_key(key))  # type: ignore[attr-defined]
        trial_raw = await self._redis.get(self._trial_key(key))  # type: ignore[attr-defined]
        if trial_raw is not None and not self._owns_trial(trial_raw, trial_token):
            return  # stale outcome: the trial holder still decides
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
