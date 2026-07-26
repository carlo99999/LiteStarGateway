"""Response-cache adapter selection (Plan 04 Phase 1: Redis or in-memory).

`build_response_cache` mirrors `build_rate_limiter`/`build_circuit_breaker`:
Redis-backed (shared across replicas) when `settings.redis_url` is set, else
the in-memory, single-replica `OrderedDict` LRU fallback.
"""

from __future__ import annotations

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports.response_cache import ResponseCache
from litestar_gateway.infrastructure.cache.in_memory import InMemoryResponseCache
from litestar_gateway.infrastructure.cache.redis import RedisResponseCache


def build_response_cache(settings: Settings) -> ResponseCache:
    """Redis-backed when REDIS_URL is set (shared across replicas), else in-memory."""
    if settings.redis_url:
        from redis.asyncio import Redis

        return RedisResponseCache(Redis.from_url(settings.redis_url))
    return InMemoryResponseCache(max_entries=settings.response_cache_max_entries)


__all__ = ["InMemoryResponseCache", "RedisResponseCache", "build_response_cache"]
