"""RedisResponseCache — TTL via EXPIRE, tenant-namespaced keys (Plan 04 Phase 1).

Mirrors `tests/misc/test_in_memory_response_cache.py`'s style for the
Redis-backed tier, against a hand-rolled `FakeRedis` (same pattern as
`tests/routing/test_circuit_breaker.py`'s `FakeRedis`): just enough surface
(GET/SET) for `RedisResponseCache` to exercise, with `expirations` recorded
so the TTL wiring itself is asserted rather than real Redis expiry."""

from __future__ import annotations

from uuid import uuid4

from litestar_gateway.domain.ports.response_cache import CachedResponse, CacheKey
from litestar_gateway.infrastructure.cache.redis import RedisResponseCache

TEAM = uuid4()
OTHER_TEAM = uuid4()


class FakeRedis:
    """Hand-rolled fake covering the small surface `RedisResponseCache` needs:
    GET/SET (with `ex=`) — enough to prove key namespacing and TTL wiring
    without a real Redis server."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True


def _key(team_id: object = TEAM, digest: str = "a") -> CacheKey:
    return CacheKey(team_id=team_id, api_key_id=None, digest=digest)  # type: ignore[arg-type]


async def test_put_then_get_returns_the_stored_value() -> None:
    cache = RedisResponseCache(FakeRedis())
    key = _key()
    value = CachedResponse(body={"id": "x"}, prompt_tokens=1, completion_tokens=2)

    await cache.put(key, value, ttl_s=60)

    assert await cache.get(key) == value


async def test_miss_returns_none() -> None:
    cache = RedisResponseCache(FakeRedis())
    assert await cache.get(_key(digest="missing")) is None


async def test_put_sets_the_ttl_via_redis_expire() -> None:
    client = FakeRedis()
    cache = RedisResponseCache(client)
    key = _key()
    value = CachedResponse(body={}, prompt_tokens=0, completion_tokens=0)

    await cache.put(key, value, ttl_s=120)

    assert client.expirations[key.redis_key()] == 120


async def test_a_different_team_never_reads_another_teams_entry() -> None:
    """Tenant isolation (design §3): the same digest under a different
    `team_id` must miss — the namespace lives in the literal Redis key."""
    client = FakeRedis()
    cache = RedisResponseCache(client)
    key_a = _key(team_id=TEAM, digest="same")
    key_b = _key(team_id=OTHER_TEAM, digest="same")
    value = CachedResponse(body={"id": "a"}, prompt_tokens=1, completion_tokens=1)

    await cache.put(key_a, value, ttl_s=60)

    assert key_a.redis_key() != key_b.redis_key()
    assert await cache.get(key_b) is None
    assert await cache.get(key_a) == value


async def test_two_adapters_sharing_one_fake_redis_client_see_each_others_writes() -> None:
    """The "shared across replicas" property (Plan 04 Phase 1 Done-when):
    two separate `RedisResponseCache` instances backed by the same client
    behave like two replicas sharing one Redis."""
    client = FakeRedis()
    cache_1 = RedisResponseCache(client)
    cache_2 = RedisResponseCache(client)
    key = _key(digest="shared")
    value = CachedResponse(body={"id": "x"}, prompt_tokens=3, completion_tokens=4)

    await cache_1.put(key, value, ttl_s=60)

    assert await cache_2.get(key) == value
