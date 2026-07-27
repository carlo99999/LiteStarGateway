"""CircuitBreaker adapters — closed -> open -> half-open state machine
(Plan 05 Phase 3)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator

from litestar_gateway.infrastructure.circuit_breaker import (
    InMemoryCircuitBreaker,
    RedisCircuitBreaker,
)


class SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class MutableClock:
    """A clock a test can advance by hand, shared between the breaker and the
    fake store so a TTL means the same thing to both."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeRedis:
    """Hand-rolled fake covering the small surface RedisCircuitBreaker needs:
    INCR/EXPIRE for the failure counter, SET NX EX for the single half-open
    trial claim, and GET/DELETE for reading/clearing state.

    TTLs are ENFORCED (ISSUE-029): the previous fake recorded expirations and
    never applied them, so the open marker never disappeared and the tests
    stayed green against an adapter that broke as soon as a real Redis expired
    the key. `clock` defaults to wall time, which is effectively "nothing
    expires" for a fast test; pass a `MutableClock` to control expiry."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self._expires_at: dict[str, float] = {}
        self._clock: Callable[[], float] = clock or time.time

    def _evict_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and self._clock() >= expires_at:
            self.store.pop(key, None)
            self.expirations.pop(key, None)
            self._expires_at.pop(key, None)

    def _set_ttl(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds
        self._expires_at[key] = self._clock() + seconds

    async def incr(self, key: str) -> int:
        self._evict_if_expired(key)
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> None:
        self._set_ttl(key, seconds)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self._evict_if_expired(key)
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self._set_ttl(key, ex)
        return True

    async def get(self, key: str) -> str | None:
        self._evict_if_expired(key)
        return self.store.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)
            self.expirations.pop(key, None)
            self._expires_at.pop(key, None)


async def test_allows_by_default() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=3, cooldown_seconds=30)
    assert await breaker.allow("model:x") is True


async def test_trips_after_consecutive_failures_reach_threshold() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0)
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    assert await breaker.allow("model:x") is True  # 2 failures, still under threshold
    await breaker.record_failure("model:x")
    assert await breaker.allow("model:x") is False  # 3rd failure trips it


async def test_a_success_before_threshold_resets_the_counter() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0)
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    await breaker.record_success("model:x")
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    assert await breaker.allow("model:x") is True  # only 2 consecutive since the success


async def test_allow_returns_true_again_after_cooldown_elapses() -> None:
    clock = SequenceClock([0.0, 0.0, 30.0])
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is False  # t=0, still cooling down
    assert await breaker.allow("model:x") is True  # t=30, half-open trial granted


async def test_only_one_half_open_trial_is_granted_at_a_time() -> None:
    clock = SequenceClock([0.0, 30.0, 30.0])
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is True  # t=30, half-open trial granted
    assert await breaker.allow("model:x") is False  # concurrent caller, no second trial


async def test_a_failure_during_the_half_open_trial_reopens_with_a_fresh_cooldown() -> None:
    clock = SequenceClock([0.0, 30.0, 30.0, 40.0, 60.0])
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is True  # t=30, half-open trial
    await breaker.record_failure("model:x")  # trial fails at t=30, re-opens
    assert await breaker.allow("model:x") is False  # t=40, still cooling down (until t=60)
    assert await breaker.allow("model:x") is True  # t=60, fresh cooldown elapsed


async def test_a_success_during_the_half_open_trial_closes_and_resets() -> None:
    clock = SequenceClock([0.0, 30.0, 30.0, 30.0])
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is True  # t=30, half-open trial
    await breaker.record_success("model:x")  # trial succeeds -> closed
    assert await breaker.allow("model:x") is True  # t=30, closed


async def test_keys_are_independent() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=lambda: 0.0)
    await breaker.record_failure("model:a")
    assert await breaker.allow("model:a") is False
    assert await breaker.allow("model:b") is True


async def test_redis_allows_by_default() -> None:
    breaker = RedisCircuitBreaker(FakeRedis(), failure_threshold=3, cooldown_seconds=30)
    assert await breaker.allow("model:x") is True


async def test_redis_trips_after_consecutive_failures_reach_threshold() -> None:
    client = FakeRedis()
    breaker = RedisCircuitBreaker(
        client, failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0
    )
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    assert await breaker.allow("model:x") is True
    await breaker.record_failure("model:x")
    assert await breaker.allow("model:x") is False


async def test_redis_success_resets_the_failure_counter() -> None:
    client = FakeRedis()
    breaker = RedisCircuitBreaker(
        client, failure_threshold=2, cooldown_seconds=30, clock=lambda: 0.0
    )
    await breaker.record_failure("model:x")
    await breaker.record_success("model:x")
    await breaker.record_failure("model:x")
    assert await breaker.allow("model:x") is True  # only 1 consecutive since the success


async def test_redis_allows_after_cooldown_and_grants_only_one_trial() -> None:
    client = FakeRedis()
    clock = SequenceClock([0.0, 0.0, 30.0, 30.0])
    breaker = RedisCircuitBreaker(client, failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is False  # t=0, cooling down
    assert await breaker.allow("model:x") is True  # t=30, trial claimed
    assert await breaker.allow("model:x") is False  # t=30, second caller loses the race


async def test_redis_failure_during_half_open_trial_reopens() -> None:
    client = FakeRedis()
    clock = SequenceClock([0.0, 30.0, 30.0, 40.0, 60.0])
    breaker = RedisCircuitBreaker(client, failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is True  # t=30, trial granted
    await breaker.record_failure("model:x")  # trial fails, re-opens
    assert await breaker.allow("model:x") is False  # t=40, cooling down again
    assert await breaker.allow("model:x") is True  # t=60, fresh cooldown elapsed


async def test_redis_success_during_half_open_trial_closes() -> None:
    client = FakeRedis()
    clock = SequenceClock([0.0, 30.0, 30.0, 30.0])
    breaker = RedisCircuitBreaker(client, failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert await breaker.allow("model:x") is True  # t=30, trial granted
    await breaker.record_success("model:x")  # trial succeeds -> closed
    assert await breaker.allow("model:x") is True


# ---------------------------------------------------------------------------
# ISSUE-029: the open→half-open transition must survive the cooldown expiring.
# ---------------------------------------------------------------------------


def _tripped(clock: MutableClock) -> tuple[RedisCircuitBreaker, FakeRedis]:
    redis = FakeRedis(clock=clock)
    breaker = RedisCircuitBreaker(redis, 1, 1, clock=clock)
    return breaker, redis


async def test_only_one_caller_gets_the_half_open_trial_after_the_cooldown() -> None:
    # Against a real Redis the open marker used to expire exactly when the
    # cooldown elapsed, so `allow()` read "no marker" and reported the breaker
    # closed: every replica was admitted at once and the trial was never
    # claimed — a stampede onto a provider that had just failed.
    clock = MutableClock()
    breaker, redis = _tripped(clock)
    await breaker.record_failure("m")

    assert await breaker.allow("m") is False  # cooling down

    clock.now += 1.2  # cooldown elapsed
    verdicts = [await breaker.allow("m") for _ in range(5)]

    assert verdicts.count(True) == 1
    assert redis.store.get("cb:trial:m") is not None


async def test_a_successful_trial_closes_the_breaker() -> None:
    clock = MutableClock()
    breaker, redis = _tripped(clock)
    await breaker.record_failure("m")
    clock.now += 1.2
    assert await breaker.allow("m") is True

    await breaker.record_success("m")

    assert redis.store == {}
    assert await breaker.allow("m") is True


async def test_a_failed_trial_reopens_with_a_fresh_cooldown() -> None:
    clock = MutableClock()
    breaker, _ = _tripped(clock)
    await breaker.record_failure("m")
    clock.now += 1.2
    assert await breaker.allow("m") is True

    await breaker.record_failure("m")  # the trial failed

    assert await breaker.allow("m") is False
    clock.now += 1.2
    assert await breaker.allow("m") is True  # one trial again, not a free-for-all
    assert await breaker.allow("m") is False


async def test_state_is_forgotten_after_a_long_idle_period() -> None:
    # The marker outlives the cooldown so the half-open transition can be
    # observed, but not forever: with no traffic at all there is no stampede to
    # protect against, and a stale key must not wedge a healthy provider.
    clock = MutableClock()
    breaker, redis = _tripped(clock)
    await breaker.record_failure("m")

    clock.now += 3_600

    assert await breaker.allow("m") is True
    assert redis.store.get("cb:opened:m") is None
