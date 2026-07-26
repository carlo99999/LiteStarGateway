"""Redis-backed response-cache adapter (Plan 04 Phase 1).

Mirrors `infrastructure/rate_limiter.py`'s `RedisRateLimiter`/
`build_rate_limiter` split (and `infrastructure/circuit_breaker.py`'s
`RedisCircuitBreaker`): the client is typed as `object` to avoid a hard
import at module load, so `redis` need not be installed unless
`Settings.redis_url` selects this adapter (`build_response_cache`). TTL is
enforced by Redis itself via the `SET ... EX` form (equivalent to `SETEX`,
i.e. `EXPIRE` folded into the write) rather than a second round-trip. Size
bounding is left to Redis's own `maxmemory-policy allkeys-lru` (documented
in the design, §4) rather than an app-level cap — unlike the in-memory
fallback's `OrderedDict`, Redis is already the shared, bounded store, and a
second eviction policy layered on top would just fight the first. In-memory
(`InMemoryResponseCache`) stays the single-replica fallback when no
`REDIS_URL` is configured.

Any exception here (a dropped connection, a malformed stored value) is
caught by `CompletionService._cache_get`/`_cache_put`, never here — design
§8's failure policy lives at the call site, exactly as for every other cache
tier, so this adapter does no defensive try/except of its own."""

from __future__ import annotations

import json
from typing import Any

from litestar_gateway.domain.ports.response_cache import CachedResponse, CacheKey


def _serialize(value: CachedResponse) -> str:
    return json.dumps(
        {
            "body": value.body,
            "prompt_tokens": value.prompt_tokens,
            "completion_tokens": value.completion_tokens,
        },
        ensure_ascii=False,
    )


def _deserialize(raw: str) -> CachedResponse:
    data: dict[str, Any] = json.loads(raw)
    return CachedResponse(
        body=data["body"],
        prompt_tokens=data["prompt_tokens"],
        completion_tokens=data["completion_tokens"],
    )


class RedisResponseCache:
    """Shared exact-match cache across replicas, keyed by the literal
    `CacheKey.redis_key()` string (`cache:{team_id}:{api_key_id}:{digest}`) so
    the tenant namespace is part of the Redis key itself, not just an
    in-process dataclass field."""

    def __init__(self, client: object) -> None:
        # redis.asyncio.Redis; typed as object to avoid a hard import at module load.
        self._redis = client

    async def get(self, key: CacheKey) -> CachedResponse | None:
        raw = await self._redis.get(key.redis_key())  # type: ignore[attr-defined]
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return _deserialize(raw)

    async def put(self, key: CacheKey, value: CachedResponse, ttl_s: int) -> None:
        await self._redis.set(key.redis_key(), _serialize(value), ex=ttl_s)  # type: ignore[attr-defined]
