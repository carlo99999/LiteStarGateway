"""Response-cache adapter selection (Plan 04 Phase 0: in-memory only).

`build_response_cache` mirrors `build_rate_limiter`/`build_circuit_breaker`:
Phase 1 adds a Redis-backed tier selected by `settings.redis_url`, with this
module's in-memory adapter staying the single-replica fallback.
"""

from __future__ import annotations

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports.response_cache import ResponseCache
from litestar_gateway.infrastructure.cache.in_memory import InMemoryResponseCache


def build_response_cache(settings: Settings) -> ResponseCache:
    return InMemoryResponseCache(max_entries=settings.response_cache_max_entries)


__all__ = ["InMemoryResponseCache", "build_response_cache"]
