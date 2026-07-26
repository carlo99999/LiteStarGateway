"""Port — per-key circuit breaker (closed / open / half-open)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CircuitBreaker(Protocol):
    """Tracks consecutive failures per key and short-circuits a key that has
    failed repeatedly for a cooldown period, then grants exactly one
    half-open trial before deciding whether to close or re-open. Implementations
    must be safe under concurrency."""

    async def allow(self, key: str) -> bool: ...
    async def record_failure(self, key: str) -> None: ...
    async def record_success(self, key: str) -> None: ...
