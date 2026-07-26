"""Port — the gateway-wide response cache (Plan 04 Phase 0: exact-match only).

`ResponseCache` sits behind `CompletionService._dispatch`'s single integration
point: a lookup immediately before the provider call, a write immediately
after settlement. Never a dependency of the money path — see design §8;
callers must treat any exception from `get`/`put` as a miss/no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class CacheKey:
    """A tenant-scoped, canonicalized cache key.

    `team_id` + `api_key_id` are the tenant namespace, derived server-side from
    the authenticated principal — never client-supplied (design §3, a hard
    invariant). `digest` is the hash of the canonicalized request view (see
    `domain.response_cache_key.derive_cache_key`). Two keys are equal — and
    thus a cache hit — only when every field matches, so a digest collision
    across tenants still misses.
    """

    team_id: UUID
    api_key_id: UUID | None
    digest: str

    def redis_key(self) -> str:
        """The `cache:{team_id}:{api_key_id}:{digest}` form future Redis-backed
        tiers (Phase 1) will use as the literal store key. Unused by the
        in-memory adapter, which keys on this dataclass directly."""
        return f"cache:{self.team_id}:{self.api_key_id}:{self.digest}"


@dataclass(frozen=True)
class CachedResponse:
    """A stored provider response body plus its authoritative token usage, so
    a later hit can be metered at the real counts (design §6) rather than
    re-running `_parse_usage` (which would compute a real, non-zero cost)."""

    body: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int


@runtime_checkable
class ResponseCache(Protocol):
    """Two tiers may sit behind this port (exact-match now, semantic in Phase
    2); Phase 0 wires only the exact-match in-memory adapter. `get` returns
    `None` on a miss; `put` stores `value` for `ttl_s` seconds. Implementations
    must give both a small hard time budget — a stalled backing store must
    become a miss, not added latency (design §8)."""

    async def get(self, key: CacheKey) -> CachedResponse | None: ...

    async def put(self, key: CacheKey, value: CachedResponse, ttl_s: int) -> None: ...


@runtime_checkable
class SemanticResponseCache(Protocol):
    """The semantic tier (Plan 04 Phase 2): tried only when the exact-match
    tier (above) has already missed and the model separately opted in. `find`
    receives an already-computed query embedding and returns the closest
    stored entry at/above `threshold` *within the caller's own tenant scope*
    — `team_id`/`api_key_id` are the same hard-invariant namespace as
    `CacheKey` (design §3); implementations must never search or match across
    them. `add` stores a fresh entry's embedding alongside its
    `CachedResponse` for future lookups. Same failure policy as
    `ResponseCache` (design §8): callers must treat any exception from
    `find`/`add` as a miss/no-op."""

    async def find(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        vector: list[float],
        threshold: float,
    ) -> CachedResponse | None: ...

    async def add(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        vector: list[float],
        value: CachedResponse,
        ttl_s: int,
    ) -> None: ...
