"""In-memory semantic response-cache adapter (Plan 04 Phase 2).

Bounded, TTL-aware, per-scope list of (vector, `CachedResponse`) pairs — no
external vector DB (an explicit non-goal, design doc); a linear cosine scan is
fine at the small bounded size this store keeps per scope, mirroring the S3
route cache's in-process, no-vector-DB approach
(`application/routing/embeddings.py`). Isolation is structural: entries live in
a bucket keyed by the exact `SemanticScope`, so a lookup can never see vectors
from another tenant, another model, another operation or another request
contract, regardless of similarity (design §3 and ISSUE-023).
"""

from __future__ import annotations

import itertools
import time
from collections import OrderedDict
from collections.abc import Callable

from litestar_gateway.domain.ports.response_cache import CachedResponse, SemanticScope
from litestar_gateway.domain.response_cache_semantic import cosine_similarity

# Small and process-local by design (no vector DB); bounds memory and keeps the
# linear cosine scan cheap per scope, mirroring embeddings.py's MAX_CACHE_ENTRIES.
DEFAULT_MAX_ENTRIES_PER_TENANT = 50

# The per-scope bound above says nothing about how many scopes exist, and a
# scope is created by every distinct (team, api key, model, operation, request
# contract) combination — a team admin can mint API keys, so the count is
# attacker-influenced (ISSUE-024). This is the actual memory ceiling: scopes are
# evicted least-recently-used first, exactly like the per-tenant list.
DEFAULT_MAX_SCOPES = 512

# How many of the least-recently-used scopes each `add` inspects for expiry.
# `find` only ever prunes the scope it was asked about, so a scope nobody looks
# up again would otherwise keep its bodies and vectors resident forever. A
# constant budget keeps the sweep O(1) amortized instead of scanning the store.
_SWEEP_BUDGET = 8

_Entry = tuple[float, list[float], CachedResponse]  # (expires_at, vector, value)


class InMemorySemanticResponseCache:
    """Process-local, single-replica semantic tier. No Redis-backed variant
    exists in this phase (no external vector DB is a design non-goal); this
    adapter is used regardless of `Settings.redis_url`."""

    def __init__(
        self,
        *,
        max_entries_per_tenant: int = DEFAULT_MAX_ENTRIES_PER_TENANT,
        max_scopes: int = DEFAULT_MAX_SCOPES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_entries_per_tenant = max_entries_per_tenant
        self._max_scopes = max_scopes
        self._clock = clock
        self._buckets: OrderedDict[SemanticScope, list[_Entry]] = OrderedDict()

    async def find(
        self,
        scope: SemanticScope,
        vector: list[float],
        threshold: float,
    ) -> CachedResponse | None:
        key = scope
        bucket = self._buckets.get(key)
        if not bucket:
            return None
        now = self._clock()
        live: list[_Entry] = []
        best_score = -1.0
        best_value: CachedResponse | None = None
        for expires_at, stored_vector, value in bucket:
            if expires_at <= now:
                continue
            live.append((expires_at, stored_vector, value))
            score = cosine_similarity(vector, stored_vector)
            if score >= threshold and score > best_score:
                best_score, best_value = score, value
        if live:
            self._buckets[key] = live
            self._buckets.move_to_end(key)  # LRU recency: a read counts as use
        else:
            self._buckets.pop(key, None)
        return best_value

    async def add(
        self,
        scope: SemanticScope,
        vector: list[float],
        value: CachedResponse,
        ttl_s: int,
    ) -> None:
        key = scope
        self._sweep_expired()
        bucket = self._buckets.setdefault(key, [])
        bucket.append((self._clock() + ttl_s, vector, value))
        overflow = len(bucket) - self._max_entries_per_tenant
        if overflow > 0:
            del bucket[:overflow]
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_scopes:
            self._buckets.popitem(last=False)  # least recently used scope

    def _sweep_expired(self) -> None:
        """Drop fully-expired scopes among the least recently used ones.

        Bounded work per call: the LRU end is where a scope that stopped being
        used ends up, which is exactly the population that would otherwise
        never be revisited and never be pruned."""
        now = self._clock()
        for key in list(itertools.islice(self._buckets, _SWEEP_BUDGET)):
            if all(expires_at <= now for expires_at, _, _ in self._buckets[key]):
                del self._buckets[key]
