"""CircuitBreaker adapters — closed -> open -> half-open state machine
(Plan 05 Phase 3)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Iterator

import pytest
from support.doubles import FakeRedis, MutableClock
from support.redis import REDIS_TEST_URL, requires_redis

from litestar_gateway.infrastructure.circuit_breaker import (
    InMemoryCircuitBreaker,
    RedisCircuitBreaker,
)


class SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


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


# ---------------------------------------------------------------------------
# Against a real Redis: the semantics a Python double cannot vouch for.
# ---------------------------------------------------------------------------


@requires_redis
async def test_the_open_marker_really_outlives_the_cooldown_in_redis() -> None:
    """ISSUE-029 in its original form: server-side TTL expiry, not a fake's.
    With the marker gone at exactly the cooldown, `allow()` read "closed" and
    admitted everyone at once."""
    from redis.asyncio import Redis

    client = Redis.from_url(str(REDIS_TEST_URL))
    key = f"conformance-{uuid.uuid4().hex[:8]}"
    try:
        breaker = RedisCircuitBreaker(client, failure_threshold=1, cooldown_seconds=1)
        await breaker.record_failure(key)
        assert (await breaker.allow(key)).allowed is False  # cooling down

        await asyncio.sleep(1.2)  # real time: the server expires its own keys
        verdicts = [await breaker.allow(key) for _ in range(5)]

        granted = [v for v in verdicts if v.allowed]
        assert len(granted) == 1  # exactly one half-open trial, fleet-wide
        assert granted[0].trial_token is not None
    finally:
        await client.delete(f"cb:failures:{key}", f"cb:opened:{key}", f"cb:trial:{key}")
        await client.aclose()


@requires_redis
async def test_a_stale_outcome_cannot_resolve_the_trial_in_redis() -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(str(REDIS_TEST_URL))
    key = f"conformance-{uuid.uuid4().hex[:8]}"
    try:
        breaker = RedisCircuitBreaker(client, failure_threshold=1, cooldown_seconds=1)
        await breaker.record_failure(key)
        await asyncio.sleep(1.2)
        lease = await breaker.allow(key)
        assert lease.trial_token is not None

        await breaker.record_success(key)  # a request admitted before the open

        assert (await breaker.allow(key)).allowed is False  # gate still shut
        await breaker.record_success(key, lease.trial_token)
        assert (await breaker.allow(key)).allowed is True
    finally:
        await client.delete(f"cb:failures:{key}", f"cb:opened:{key}", f"cb:trial:{key}")
        await client.aclose()
