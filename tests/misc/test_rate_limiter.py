"""InMemoryRateLimiter — fixed-window counter behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator

from litestar_gateway.infrastructure.rate_limiter import InMemoryRateLimiter, RedisRateLimiter


class SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        return count

    async def expire(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds


async def test_allows_up_to_limit_then_blocks() -> None:
    rl = InMemoryRateLimiter(clock=lambda: 58.25)
    first = await rl.hit("team:x", limit=2)
    second = await rl.hit("team:x", limit=2)
    third = await rl.hit("team:x", limit=2)

    assert first.allowed and first.retry_after == 0
    assert second.allowed
    assert not third.allowed
    assert third.retry_after == 2


async def test_keys_are_independent() -> None:
    rl = InMemoryRateLimiter(clock=lambda: 0.0)
    assert (await rl.hit("a", limit=1)).allowed
    assert not (await rl.hit("a", limit=1)).allowed
    # A different key has its own counter.
    assert (await rl.hit("b", limit=1)).allowed


async def test_prunes_historical_keys_on_the_next_hit() -> None:
    clock = SequenceClock([0.0] * 1_000 + [60.0])
    rl = InMemoryRateLimiter(clock=clock)

    for index in range(1_000):
        await rl.hit(f"retired:{index}", limit=1)
    assert len(rl._counts) == 1_000

    await rl.hit("current", limit=1)

    assert set(rl._counts) == {"current"}


async def test_does_not_prune_a_bucket_before_its_expiry() -> None:
    clock = SequenceClock([0.0, 59.999, 59.999])
    rl = InMemoryRateLimiter(clock=clock)

    assert (await rl.hit("active", limit=1)).allowed
    assert (await rl.hit("trigger", limit=1)).allowed

    blocked = await rl.hit("active", limit=1)
    assert not blocked.allowed
    assert set(rl._counts) == {"active", "trigger"}


async def test_pruning_preserves_unexpired_mixed_windows() -> None:
    clock = SequenceClock([0.0, 0.0, 10.0, 10.0])
    rl = InMemoryRateLimiter(clock=clock)

    assert (await rl.hit("short", limit=1, window_seconds=10)).allowed
    assert (await rl.hit("long", limit=1, window_seconds=120)).allowed
    assert (await rl.hit("short", limit=1, window_seconds=10)).allowed

    long_second = await rl.hit("long", limit=1, window_seconds=120)
    assert not long_second.allowed
    assert set(rl._counts) == {"short", "long"}


async def test_concurrent_hits_still_enforce_the_exact_limit_during_pruning() -> None:
    stale_count = 100
    request_count = 50
    limit = 7
    clock = SequenceClock([0.0] * stale_count + [60.0] * request_count)
    rl = InMemoryRateLimiter(clock=clock)
    for index in range(stale_count):
        await rl.hit(f"stale:{index}", limit=1)

    decisions = await asyncio.gather(
        *(rl.hit("current", limit=limit) for _ in range(request_count))
    )

    assert sum(decision.allowed for decision in decisions) == limit
    assert set(rl._counts) == {"current"}


async def test_changing_window_size_starts_a_new_counter() -> None:
    clock = SequenceClock([100.0, 100.0])
    rl = InMemoryRateLimiter(clock=clock)

    assert (await rl.hit("key", limit=1, window_seconds=100)).allowed
    assert (await rl.hit("key", limit=1, window_seconds=60)).allowed


async def test_redis_separates_counters_with_different_window_sizes() -> None:
    client = FakeRedis()
    rl = RedisRateLimiter(client, clock=lambda: 100.0)

    assert (await rl.hit("key", limit=1, window_seconds=100)).allowed
    assert (await rl.hit("key", limit=1, window_seconds=60)).allowed
    assert client.counts == {"rl:key:100:1": 1, "rl:key:60:1": 1}
    assert client.expirations == {"rl:key:100:1": 101, "rl:key:60:1": 61}
