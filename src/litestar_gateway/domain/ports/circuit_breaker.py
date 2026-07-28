"""Port — per-key circuit breaker (closed / open / half-open)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class BreakerLease:
    """The verdict of one `allow()` call, plus proof of what it granted.

    `trial_token` is non-`None` **only** when this call was the one that took
    the single half-open trial. It is what ties an outcome to the attempt that
    earned the right to decide the transition: without it, a request admitted
    before the breaker opened could report success minutes later and close the
    gate while the real trial was still in flight — reopening the stampede in a
    different interleaving (ISSUE-033).
    """

    allowed: bool
    trial_token: str | None = None


@runtime_checkable
class CircuitBreaker(Protocol):
    """Tracks consecutive failures per key and short-circuits a key that has
    failed repeatedly for a cooldown period, then grants exactly one
    half-open trial before deciding whether to close or re-open. Implementations
    must be safe under concurrency.

    Outcomes carry the `trial_token` from their own `allow()`. A tokenless
    outcome is ordinary traffic: it may reset the failure count on a closed
    breaker and count towards tripping it, but it can never resolve a
    half-open trial — only its holder decides."""

    async def allow(self, key: str) -> BreakerLease: ...
    async def record_failure(self, key: str, trial_token: str | None = None) -> None: ...
    async def record_success(self, key: str, trial_token: str | None = None) -> None: ...
