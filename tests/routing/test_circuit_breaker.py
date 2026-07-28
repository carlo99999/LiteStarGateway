"""CircuitBreaker adapters — closed -> open -> half-open state machine
(Plan 05 Phase 3)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator

import pytest

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
    assert (await breaker.allow("model:x")).allowed is True


async def test_trips_after_consecutive_failures_reach_threshold() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0)
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    assert (await breaker.allow("model:x")).allowed is True  # 2 failures, still under threshold
    await breaker.record_failure("model:x")
    assert (await breaker.allow("model:x")).allowed is False  # 3rd failure trips it


async def test_a_success_before_threshold_resets_the_counter() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0)
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    await breaker.record_success("model:x")
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    assert (await breaker.allow("model:x")).allowed is True  # only 2 consecutive since the success


async def test_allow_returns_true_again_after_cooldown_elapses() -> None:
    clock = SequenceClock([0.0, 0.0, 30.0])
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert (await breaker.allow("model:x")).allowed is False  # t=0, still cooling down
    assert (await breaker.allow("model:x")).allowed is True  # t=30, half-open trial granted


async def test_only_one_half_open_trial_is_granted_at_a_time() -> None:
    clock = SequenceClock([0.0, 30.0, 30.0])
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert (await breaker.allow("model:x")).allowed is True  # t=30, half-open trial granted
    assert (await breaker.allow("model:x")).allowed is False  # concurrent caller, no second trial


async def test_keys_are_independent() -> None:
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=lambda: 0.0)
    await breaker.record_failure("model:a")
    assert (await breaker.allow("model:a")).allowed is False
    assert (await breaker.allow("model:b")).allowed is True


async def test_redis_allows_by_default() -> None:
    breaker = RedisCircuitBreaker(FakeRedis(), failure_threshold=3, cooldown_seconds=30)
    assert (await breaker.allow("model:x")).allowed is True


async def test_redis_trips_after_consecutive_failures_reach_threshold() -> None:
    client = FakeRedis()
    breaker = RedisCircuitBreaker(
        client, failure_threshold=3, cooldown_seconds=30, clock=lambda: 0.0
    )
    await breaker.record_failure("model:x")
    await breaker.record_failure("model:x")
    assert (await breaker.allow("model:x")).allowed is True
    await breaker.record_failure("model:x")
    assert (await breaker.allow("model:x")).allowed is False


async def test_redis_success_resets_the_failure_counter() -> None:
    client = FakeRedis()
    breaker = RedisCircuitBreaker(
        client, failure_threshold=2, cooldown_seconds=30, clock=lambda: 0.0
    )
    await breaker.record_failure("model:x")
    await breaker.record_success("model:x")
    await breaker.record_failure("model:x")
    assert (await breaker.allow("model:x")).allowed is True  # only 1 consecutive since the success


async def test_redis_allows_after_cooldown_and_grants_only_one_trial() -> None:
    client = FakeRedis()
    clock = SequenceClock([0.0, 0.0, 30.0, 30.0])
    breaker = RedisCircuitBreaker(client, failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("model:x")  # trips open at t=0
    assert (await breaker.allow("model:x")).allowed is False  # t=0, cooling down
    assert (await breaker.allow("model:x")).allowed is True  # t=30, trial claimed
    assert (await breaker.allow("model:x")).allowed is False  # t=30, second caller loses the race


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

    assert (await breaker.allow("m")).allowed is False  # cooling down

    clock.now += 1.2  # cooldown elapsed
    verdicts = [(await breaker.allow("m")).allowed for _ in range(5)]

    assert verdicts.count(True) == 1
    assert redis.store.get("cb:trial:m") is not None


async def test_state_is_forgotten_after_a_long_idle_period() -> None:
    # The marker outlives the cooldown so the half-open transition can be
    # observed, but not forever: with no traffic at all there is no stampede to
    # protect against, and a stale key must not wedge a healthy provider.
    clock = MutableClock()
    breaker, redis = _tripped(clock)
    await breaker.record_failure("m")

    clock.now += 3_600

    assert (await breaker.allow("m")).allowed is True
    assert redis.store.get("cb:opened:m") is None


# ---------------------------------------------------------------------------
# ISSUE-033: only the trial's own outcome decides the transition.
#
# One conformance suite over BOTH adapters. The two implementations had one
# test file each, which is how the Redis one drifted from the in-memory one
# unnoticed (ISSUE-029); a shared suite is the cheapest device against that.
# ---------------------------------------------------------------------------

_ADAPTERS = ("in_memory", "redis")


def _breaker(kind: str, clock: MutableClock):
    if kind == "in_memory":
        return InMemoryCircuitBreaker(1, 1, clock=clock)
    return RedisCircuitBreaker(FakeRedis(clock=clock), 1, 1, clock=clock)


async def _open_and_claim_trial(breaker, clock: MutableClock) -> str:
    """Trip the breaker, let the cooldown elapse, take the single trial."""
    await breaker.record_failure("m")
    assert (await breaker.allow("m")).allowed is False  # cooling down
    clock.now += 1.2
    lease = await breaker.allow("m")
    assert lease.allowed is True
    assert lease.trial_token is not None
    return lease.trial_token


@pytest.mark.parametrize("kind", _ADAPTERS)
async def test_a_stale_success_does_not_close_a_breaker_mid_trial(kind: str) -> None:
    # Request A was admitted before the breaker opened and finishes after T
    # took the trial. Its success must not remove the gate: nothing about A
    # says the provider recovered.
    clock = MutableClock()
    breaker = _breaker(kind, clock)
    await _open_and_claim_trial(breaker, clock)

    await breaker.record_success("m")  # A, no trial token

    verdicts = [(await breaker.allow("m")).allowed for _ in range(5)]
    assert verdicts == [False] * 5  # the gate is still shut, no second trial


@pytest.mark.parametrize("kind", _ADAPTERS)
async def test_a_stale_failure_does_not_reopen_over_a_live_trial(kind: str) -> None:
    clock = MutableClock()
    breaker = _breaker(kind, clock)
    token = await _open_and_claim_trial(breaker, clock)

    await breaker.record_failure("m")  # A, no trial token

    # T's own success is still what decides, and it closes the breaker.
    await breaker.record_success("m", token)
    assert (await breaker.allow("m")).allowed is True
    assert (await breaker.allow("m")).allowed is True  # closed, not half-open


@pytest.mark.parametrize("kind", _ADAPTERS)
async def test_the_trials_own_success_closes_the_breaker(kind: str) -> None:
    clock = MutableClock()
    breaker = _breaker(kind, clock)
    token = await _open_and_claim_trial(breaker, clock)

    await breaker.record_success("m", token)

    assert (await breaker.allow("m")).allowed is True


@pytest.mark.parametrize("kind", _ADAPTERS)
async def test_the_trials_own_failure_reopens_with_a_fresh_cooldown(kind: str) -> None:
    clock = MutableClock()
    breaker = _breaker(kind, clock)
    token = await _open_and_claim_trial(breaker, clock)

    await breaker.record_failure("m", token)

    assert (await breaker.allow("m")).allowed is False
    clock.now += 1.2
    assert (await breaker.allow("m")).allowed is True  # one trial again
    assert (await breaker.allow("m")).allowed is False


@pytest.mark.parametrize("kind", _ADAPTERS)
async def test_a_wrong_token_is_treated_as_stale(kind: str) -> None:
    clock = MutableClock()
    breaker = _breaker(kind, clock)
    await _open_and_claim_trial(breaker, clock)

    await breaker.record_success("m", "not-the-trial-token")

    assert (await breaker.allow("m")).allowed is False


@pytest.mark.parametrize("kind", _ADAPTERS)
async def test_an_ordinary_success_still_resets_the_failure_count(kind: str) -> None:
    # No regression for the common path: a tokenless success on a CLOSED
    # breaker is exactly what makes only *consecutive* failures count.
    clock = MutableClock()
    breaker = _breaker(kind, clock)

    await breaker.record_success("m")

    assert (await breaker.allow("m")).allowed is True
