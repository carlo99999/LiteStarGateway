"""Rate-limiter adapters — fixed-window request counters.

In-memory by default (per-process); Redis-backed when a REDIS_URL is configured,
so the counter is shared across replicas. Both implement the `RateLimiter` port.
The window is aligned to wall-clock (bucket = floor(now / window)), so all
replicas agree on the current bucket without coordination.
"""

from __future__ import annotations

import asyncio
import time

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import RateLimitDecision, RateLimiter


def _retry_after(window_seconds: int) -> int:
    """Seconds until the current fixed window rolls over."""
    return window_seconds - int(time.time()) % window_seconds


class InMemoryRateLimiter:
    """Process-local counter. Correct for a single replica; for multi-replica
    deployments configure Redis so the limit is enforced fleet-wide."""

    def __init__(self) -> None:
        # key -> (bucket, count); one entry per active key, reset when its bucket
        # rolls, so memory is bounded by the number of distinct keys.
        self._counts: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision:
        bucket = int(time.time()) // window_seconds
        async with self._lock:
            stored_bucket, count = self._counts.get(key, (bucket, 0))
            count = count + 1 if stored_bucket == bucket else 1
            self._counts[key] = (bucket, count)
        allowed = count <= limit
        return RateLimitDecision(
            allowed=allowed, retry_after=0 if allowed else _retry_after(window_seconds)
        )


class RedisRateLimiter:
    """Shared counter across replicas via atomic INCR on a per-window key."""

    def __init__(self, client: object) -> None:
        # redis.asyncio.Redis; typed as object to avoid a hard import at module load.
        self._redis = client

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision:
        bucket = int(time.time()) // window_seconds
        redis_key = f"rl:{key}:{bucket}"
        count = await self._redis.incr(redis_key)  # type: ignore[attr-defined]
        if count == 1:
            # Expire the bucket a little after it rolls; the tiny window between
            # INCR and EXPIRE only risks a lingering key if the process dies here.
            await self._redis.expire(redis_key, window_seconds + 1)  # type: ignore[attr-defined]
        allowed = count <= limit
        return RateLimitDecision(
            allowed=allowed, retry_after=0 if allowed else _retry_after(window_seconds)
        )


def build_rate_limiter(settings: Settings) -> RateLimiter:
    """Redis-backed when REDIS_URL is set (shared across replicas), else in-memory."""
    if settings.redis_url:
        from redis.asyncio import Redis

        return RedisRateLimiter(Redis.from_url(settings.redis_url))
    return InMemoryRateLimiter()
