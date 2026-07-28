"""A deterministic clock and a strict, TTL-faithful Redis fake."""

from __future__ import annotations

import time
from collections.abc import Callable


class MutableClock:
    """A clock a test can advance by hand, shared between the breaker and the
    fake store so a TTL means the same thing to both."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeRedis:
    """Hand-rolled fake covering the small surface RedisCircuitBreaker needs:
    INCR/EXPIRE for the failure counter, SET NX EX for the single half-open
    trial claim, and GET/DELETE for reading/clearing state.

    TTLs are ENFORCED (ISSUE-029): the previous fake recorded expirations and
    never applied them, so the open marker never disappeared and the tests
    stayed green against an adapter that broke as soon as a real Redis expired
    the key. `clock` defaults to wall time, which is effectively "nothing
    expires" for a fast test; pass a `MutableClock` to control expiry."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self._expires_at: dict[str, float] = {}
        self._clock: Callable[[], float] = clock or time.time

    def _evict_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and self._clock() >= expires_at:
            self.store.pop(key, None)
            self.expirations.pop(key, None)
            self._expires_at.pop(key, None)

    def _set_ttl(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds
        self._expires_at[key] = self._clock() + seconds

    async def incr(self, key: str) -> int:
        self._evict_if_expired(key)
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> None:
        self._set_ttl(key, seconds)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self._evict_if_expired(key)
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self._set_ttl(key, ex)
        return True

    async def get(self, key: str) -> str | None:
        self._evict_if_expired(key)
        return self.store.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)
            self.expirations.pop(key, None)
            self._expires_at.pop(key, None)
