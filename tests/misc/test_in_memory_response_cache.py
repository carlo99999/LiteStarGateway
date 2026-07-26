"""InMemoryResponseCache — TTL expiry + bounded LRU eviction (Plan 04 Phase 0)."""

from __future__ import annotations

from uuid import uuid4

from litestar_gateway.domain.ports.response_cache import CachedResponse, CacheKey
from litestar_gateway.infrastructure.cache.in_memory import InMemoryResponseCache

TEAM = uuid4()


def _key(digest: str) -> CacheKey:
    return CacheKey(team_id=TEAM, api_key_id=None, digest=digest)


async def test_put_then_get_returns_the_stored_value() -> None:
    cache = InMemoryResponseCache(max_entries=10)
    key = _key("a")
    value = CachedResponse(body={"id": "x"}, prompt_tokens=1, completion_tokens=2)

    await cache.put(key, value, ttl_s=60)

    assert await cache.get(key) == value


async def test_miss_returns_none() -> None:
    cache = InMemoryResponseCache(max_entries=10)
    assert await cache.get(_key("missing")) is None


async def test_entry_expires_after_its_ttl() -> None:
    clock_value = [0.0]
    cache = InMemoryResponseCache(max_entries=10, clock=lambda: clock_value[0])
    key = _key("a")
    value = CachedResponse(body={}, prompt_tokens=1, completion_tokens=1)

    await cache.put(key, value, ttl_s=60)
    clock_value[0] = 61.0

    assert await cache.get(key) is None


async def test_entry_is_still_live_just_before_its_ttl() -> None:
    clock_value = [0.0]
    cache = InMemoryResponseCache(max_entries=10, clock=lambda: clock_value[0])
    key = _key("a")
    value = CachedResponse(body={}, prompt_tokens=1, completion_tokens=1)

    await cache.put(key, value, ttl_s=60)
    clock_value[0] = 59.999

    assert await cache.get(key) == value


async def test_oldest_entry_is_evicted_past_max_entries() -> None:
    cache = InMemoryResponseCache(max_entries=2)
    value = CachedResponse(body={}, prompt_tokens=0, completion_tokens=0)

    await cache.put(_key("a"), value, ttl_s=60)
    await cache.put(_key("b"), value, ttl_s=60)
    await cache.put(_key("c"), value, ttl_s=60)

    assert await cache.get(_key("a")) is None
    assert await cache.get(_key("b")) == value
    assert await cache.get(_key("c")) == value


async def test_a_read_hit_refreshes_recency_so_it_survives_the_next_eviction() -> None:
    cache = InMemoryResponseCache(max_entries=2)
    value = CachedResponse(body={}, prompt_tokens=0, completion_tokens=0)

    await cache.put(_key("a"), value, ttl_s=60)
    await cache.put(_key("b"), value, ttl_s=60)
    await cache.get(_key("a"))  # "a" is now the most-recently-used
    await cache.put(_key("c"), value, ttl_s=60)

    assert await cache.get(_key("b")) is None  # "b" was the least-recently-used
    assert await cache.get(_key("a")) == value
