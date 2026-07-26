"""CircuitBreaker adapters — closed -> open -> half-open state machine
(Plan 05 Phase 3)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from litestar_gateway.infrastructure.circuit_breaker import (
    InMemoryCircuitBreaker,
    RedisCircuitBreaker,
)


class SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class FakeRedis:
    """Hand-rolled fake covering the small surface RedisCircuitBreaker needs:
    INCR/EXPIRE for the failure counter, SET NX EX for the single half-open
    trial claim, and GET/DELETE for reading/clearing state."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)
            self.expirations.pop(key, None)


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
