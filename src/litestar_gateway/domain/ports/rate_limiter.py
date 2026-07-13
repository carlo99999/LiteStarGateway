"""Port — request-rate limiting (fixed-window counters)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of counting one request against a fixed window."""

    allowed: bool
    # Seconds until the current window resets (for a `Retry-After` header). 0 when
    # allowed.
    retry_after: int


@runtime_checkable
class RateLimiter(Protocol):
    """Counts requests per key in a fixed window and reports whether the caller
    is within the limit. Implementations must count the current request even when
    it exceeds the limit (so sustained abuse keeps failing until the window
    rolls), and be safe under concurrency."""

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision: ...
