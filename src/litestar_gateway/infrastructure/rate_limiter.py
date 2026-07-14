"""Rate-limiter adapters — fixed-window request counters.

In-memory by default (per-process); Redis-backed when a REDIS_URL is configured,
so the counter is shared across replicas. Both implement the `RateLimiter` port.
The window is aligned to wall-clock (bucket = floor(now / window)), so all
replicas agree on the current bucket without coordination.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import RateLimitDecision, RateLimiter


@dataclass(frozen=True, slots=True)
class _Counter:
    window_seconds: int
    bucket: int
    count: int
    expires_at: float


def _retry_after(window_seconds: int, now: float) -> int:
    """Seconds until the current fixed window rolls over."""
    return window_seconds - int(now) % window_seconds


class InMemoryRateLimiter:
    """Process-local counter. Correct for a single replica; for multi-replica
    deployments configure Redis so the limit is enforced fleet-wide."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        # key -> (window_seconds, bucket, count, expires_at). Expired identities
        # are removed lazily on the first subsequent hit, so storage tracks only
        # identities observed in windows that are still active.
        self._counts: dict[str, _Counter] = {}
        self._next_prune_at: float | None = None
        self._clock = clock
        self._lock = asyncio.Lock()

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision:
        async with self._lock:
            now = self._clock()
            if self._next_prune_at is not None and now >= self._next_prune_at:
                self._counts = {
                    stored_key: entry
                    for stored_key, entry in self._counts.items()
                    if entry.expires_at > now
                }
                self._next_prune_at = min(
                    (entry.expires_at for entry in self._counts.values()), default=None
                )

            bucket = int(now) // window_seconds
            stored = self._counts.get(key)
            if (
                stored is not None
                and stored.window_seconds == window_seconds
                and stored.bucket == bucket
            ):
                count = stored.count + 1
                expires_at = stored.expires_at
            else:
                count = 1
                expires_at = float((bucket + 1) * window_seconds)
            self._counts[key] = _Counter(window_seconds, bucket, count, expires_at)
            if self._next_prune_at is None or expires_at < self._next_prune_at:
                self._next_prune_at = expires_at
        allowed = count <= limit
        return RateLimitDecision(
            allowed=allowed,
            retry_after=0 if allowed else _retry_after(window_seconds, now),
        )


class RedisRateLimiter:
    """Shared counter across replicas via atomic INCR on a per-window key."""

    def __init__(self, client: object, *, clock: Callable[[], float] = time.time) -> None:
        # redis.asyncio.Redis; typed as object to avoid a hard import at module load.
        self._redis = client
        self._clock = clock

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision:
        now = self._clock()
        bucket = int(now) // window_seconds
        # Window duration is part of the counter identity. Without it, callers
        # using the same logical key with different windows can share both count
        # and TTL when their bucket numbers happen to coincide.
        redis_key = f"rl:{key}:{window_seconds}:{bucket}"
        count = await self._redis.incr(redis_key)  # type: ignore[attr-defined]
        if count == 1:
            # Expire the bucket a little after it rolls; the tiny window between
            # INCR and EXPIRE only risks a lingering key if the process dies here.
            await self._redis.expire(redis_key, window_seconds + 1)  # type: ignore[attr-defined]
        allowed = count <= limit
        return RateLimitDecision(
            allowed=allowed,
            retry_after=0 if allowed else _retry_after(window_seconds, now),
        )


def build_rate_limiter(settings: Settings) -> RateLimiter:
    """Redis-backed when REDIS_URL is set (shared across replicas), else in-memory."""
    if settings.redis_url:
        from redis.asyncio import Redis

        return RedisRateLimiter(Redis.from_url(settings.redis_url))
    return InMemoryRateLimiter()
