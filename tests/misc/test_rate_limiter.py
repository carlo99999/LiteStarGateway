"""InMemoryRateLimiter — fixed-window counter behavior."""

from __future__ import annotations

from litestar_gateway.infrastructure.rate_limiter import InMemoryRateLimiter


async def test_allows_up_to_limit_then_blocks() -> None:
    rl = InMemoryRateLimiter()
    first = await rl.hit("team:x", limit=2)
    second = await rl.hit("team:x", limit=2)
    third = await rl.hit("team:x", limit=2)

    assert first.allowed and first.retry_after == 0
    assert second.allowed
    assert not third.allowed
    assert third.retry_after > 0  # seconds until the window resets


async def test_keys_are_independent() -> None:
    rl = InMemoryRateLimiter()
    assert (await rl.hit("a", limit=1)).allowed
    assert not (await rl.hit("a", limit=1)).allowed
    # A different key has its own counter.
    assert (await rl.hit("b", limit=1)).allowed
