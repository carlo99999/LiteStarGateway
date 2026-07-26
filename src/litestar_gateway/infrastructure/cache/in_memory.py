"""In-memory LRU response-cache adapter (Plan 04 Phase 0).

Process-local `OrderedDict` eviction, mirroring the S3 embeddings route cache
(`application/routing/embeddings.py`). Correct for a single replica; Phase 1
adds a Redis-backed tier behind the same `ResponseCache` port for multi-replica
sharing, with this adapter staying the fallback (same in-memory-vs-Redis split
as rate limiting and the circuit breaker).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable

from litestar_gateway.domain.ports.response_cache import CachedResponse, CacheKey


class InMemoryResponseCache:
    """Bounded, TTL-aware, process-local exact-match cache."""

    def __init__(self, *, max_entries: int, clock: Callable[[], float] = time.time) -> None:
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[float, CachedResponse]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: CacheKey) -> CachedResponse | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= self._clock():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    async def put(self, key: CacheKey, value: CachedResponse, ttl_s: int) -> None:
        async with self._lock:
            self._entries[key] = (self._clock() + ttl_s, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
