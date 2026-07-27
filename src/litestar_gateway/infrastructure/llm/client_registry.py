"""Process-owned, bounded cache of async provider SDK clients.

Every adapter operation used to construct and close its own SDK client, which
also builds and tears down its own `httpx.AsyncClient` connection pool and, on
every single call, a fresh `ssl.create_default_context()` — the single most
expensive per-request cost measured on the gateway hot path (Plan 14 Step 1).

This registry lets adapters lease a client keyed by everything that can change
its behavior (provider, credential material, endpoint, API version, region,
...) so the underlying connection pool and TLS context are built once and
reused across requests, not reconstructed per call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("litestar_gateway.llm.client_registry")


def fingerprint_material(*parts: str | None) -> str:
    """One-way identity for the exact ordered values that determine client
    behavior. May include secret material (e.g. an API key) as an input, but
    the output never lets that material be recovered — safe to use in a cache
    key, log line, or metric label. A `None` part is distinguished from an
    empty string so omitted vs. blank values never collide."""
    digest = hashlib.sha256()
    for part in parts:
        marker = b"\x00N" if part is None else b"\x00S"
        digest.update(marker)
        digest.update((part or "").encode("utf-8", errors="surrogatepass"))
    return digest.hexdigest()[:32]


@dataclass(frozen=True)
class ClientKey:
    """Cache key for one provider client.

    `fingerprint` must be derived (via `fingerprint_material`) from every
    constructor value that can change the client's behavior — credential
    material, endpoint, API version, region/project, and so on. `endpoint` is
    kept separately, in the clear, purely for observability (metric labels,
    logs): it must never itself be or contain secret material.
    """

    provider: str
    fingerprint: str
    endpoint: str = ""


@dataclass(frozen=True)
class RegistryMetrics:
    """Bounded, secret-free snapshot of registry activity."""

    hits: int
    misses: int
    creates: int
    evictions: int
    active_leases: int
    live_clients: int


@dataclass
class _Entry:
    client: Any
    created_at: float
    last_used_at: float
    leases: int = 0
    closing: bool = False
    closed: bool = False


class ClientRegistryClosed(RuntimeError):
    """Raised by `lease()` after `aclose()` has run."""


class ClientRegistry:
    """Bounded (LRU + TTL) async client cache with per-key leasing.

    - Concurrent misses on the same key build exactly one client.
    - A client is closed only after its last active lease releases — eviction,
      TTL expiry, and shutdown all defer to that instead of closing a client
      another request still holds.
    - Every close (evicted, expired, or on shutdown) runs at most once.

    Not safe to share across event loops (nor is it meant to be): one registry
    lives for the lifetime of one worker process's single event loop, same as
    the `LLMGatewayImpl` that owns it.
    """

    def __init__(
        self,
        *,
        capacity: int = 64,
        ttl_seconds: float = 3600.0,
        close: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._close = close
        self._entries: dict[ClientKey, _Entry] = {}
        # Generations displaced from `_entries` while still leased (a key was
        # reacquired while its previous entry was marked close-on-release).
        # They are no longer reusable, but they still own a connection pool and
        # a TLS context, so the registry keeps closing them: on their last
        # release, or at shutdown (ISSUE-025).
        self._retired: list[tuple[ClientKey, _Entry]] = []
        self._creation_locks: dict[ClientKey, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._creates = 0
        self._evictions = 0
        self._closed = False

    @asynccontextmanager
    async def lease(self, key: ClientKey, factory: Callable[[], Any]) -> AsyncIterator[Any]:
        """Lease the client for `key`, constructing it on first use.

        Usage: `async with registry.lease(key, lambda: Client(...)) as client:`.
        The client is never closed while this (or any concurrent) lease on the
        same key is still open, including if the calling task is cancelled
        inside the `async with` block — release still runs during unwind.
        """
        if self._closed:
            raise ClientRegistryClosed("ClientRegistry is closed")
        entry = await self._acquire(key, factory)
        try:
            yield entry.client
        finally:
            await self._release(key, entry)

    async def _acquire(self, key: ClientKey, factory: Callable[[], Any]) -> _Entry:
        hit = await self._try_hit(key)
        if hit is not None:
            return hit

        lock = await self._creation_lock(key)
        async with lock:
            # Another task may have built it while we waited for the lock.
            hit = await self._try_hit(key)
            if hit is not None:
                return hit

            self._misses += 1
            try:
                client = factory()  # never awaited: construction must stay atomic
            finally:
                # Drop the lock's slot whether construction succeeded or not,
                # so a failed factory never poisons the key for the next
                # attempt (no stale lock, no partial entry ever inserted).
                async with self._guard:
                    self._creation_locks.pop(key, None)

            now = time.monotonic()
            new_entry = _Entry(client=client, created_at=now, last_used_at=now, leases=1)
            async with self._guard:
                displaced = self._entries.get(key)
                if displaced is not None and not displaced.closed:
                    # Only a `closing` entry can still be here (`_try_hit`
                    # would have reused any other), and it is still leased —
                    # otherwise its release would already have closed it.
                    self._retired.append((key, displaced))
                self._entries[key] = new_entry
                self._creates += 1
                await self._evict_locked()
            return new_entry

    async def _try_hit(self, key: ClientKey) -> _Entry | None:
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None or entry.closing:
                return None
            entry.leases += 1
            entry.last_used_at = time.monotonic()
            self._hits += 1
            return entry

    async def _creation_lock(self, key: ClientKey) -> asyncio.Lock:
        async with self._guard:
            lock = self._creation_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._creation_locks[key] = lock
            return lock

    async def _release(self, key: ClientKey, entry: _Entry) -> None:
        async with self._guard:
            entry.leases -= 1
            if entry.leases <= 0 and (entry.closing or self._is_expired(entry)):
                await self._close_entry_locked(key, entry)

    def _is_expired(self, entry: _Entry) -> bool:
        return (time.monotonic() - entry.created_at) >= self._ttl_seconds

    async def _evict_locked(self) -> None:
        """Must be called while holding `self._guard`."""
        # Sweep TTL-expired, unleased entries first — this is the only sweep
        # point (lazy, traffic-driven), not a background timer.
        for k, e in list(self._entries.items()):
            if e.leases == 0 and not e.closing and self._is_expired(e):
                await self._close_entry_locked(k, e)

        if len(self._entries) <= self._capacity:
            return

        # LRU-evict unleased entries until back at capacity.
        unleased = sorted(
            (k for k, e in self._entries.items() if e.leases == 0 and not e.closing),
            key=lambda k: self._entries[k].last_used_at,
        )
        for k in unleased:
            if len(self._entries) <= self._capacity:
                break
            await self._close_entry_locked(k, self._entries[k])

        if len(self._entries) <= self._capacity:
            return

        # Still over capacity: every remaining entry is leased. Mark the
        # oldest for close-on-release instead of evicting it now.
        leased = sorted(self._entries.items(), key=lambda kv: kv[1].last_used_at)
        pending_closes = 0
        overage = len(self._entries) - self._capacity
        for _, e in leased:
            if pending_closes >= overage:
                break
            if not e.closing:
                e.closing = True
                pending_closes += 1

    async def _close_entry_locked(self, key: ClientKey, entry: _Entry) -> None:
        """Must be called while holding `self._guard`."""
        if entry.closed:
            return
        entry.closed = True
        # Remove the slot only if it still holds THIS entry: a newer generation
        # may have taken the key while this one was draining (ISSUE-025).
        if self._entries.get(key) is entry:
            del self._entries[key]
        self._retired = [(k, e) for k, e in self._retired if e is not entry]
        self._evictions += 1
        if self._close is not None:
            try:
                await self._close(entry.client)
            except Exception:
                logger.exception(
                    "client_registry: error closing client (provider=%s, endpoint=%s)",
                    key.provider,
                    key.endpoint,
                )

    async def aclose(self) -> None:
        """Close every retained client exactly once. Idempotent."""
        async with self._guard:
            self._closed = True
            entries = list(self._entries.items()) + list(self._retired)
        for key, entry in entries:
            async with self._guard:
                if entry.closed:
                    continue
                entry.closed = True
                if self._entries.get(key) is entry:
                    del self._entries[key]
                self._retired = [(k, e) for k, e in self._retired if e is not entry]
            if self._close is not None:
                try:
                    await self._close(entry.client)
                except Exception:
                    logger.exception(
                        "client_registry: error closing client on shutdown "
                        "(provider=%s, endpoint=%s)",
                        key.provider,
                        key.endpoint,
                    )

    def metrics(self) -> RegistryMetrics:
        # Retired generations are still live clients holding live leases until
        # their last holder releases, so they count in both figures.
        live = list(self._entries.values()) + [e for _, e in self._retired]
        active_leases = sum(e.leases for e in live)
        return RegistryMetrics(
            hits=self._hits,
            misses=self._misses,
            creates=self._creates,
            evictions=self._evictions,
            active_leases=active_leases,
            live_clients=len(live),
        )
