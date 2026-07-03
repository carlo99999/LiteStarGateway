"""Distributed-lock adapters for cross-replica coordination.

With `REDIS_URL` configured, `RedisDistributedLock` uses redis-py's `SET NX PX` +
Lua-release lock so only one replica runs a guarded section (e.g. key rotation).
Without Redis, `NoOpDistributedLock` always "acquires" — correct for a single
instance; multiple replicas need Redis to actually serialize.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from redis.asyncio import Redis

from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import DistributedLock

logger = logging.getLogger("litestar_gateway.locks")


class NoOpDistributedLock:
    """No coordination — always acquires. Fine for a single instance; configure
    REDIS_URL to actually serialize across replicas."""

    @asynccontextmanager
    async def hold(self, name: str, *, ttl: timedelta) -> AsyncIterator[bool]:
        yield True


class RedisDistributedLock:
    def __init__(self, url: str) -> None:
        self._url = url

    @asynccontextmanager
    async def hold(self, name: str, *, ttl: timedelta) -> AsyncIterator[bool]:
        # A short-lived client per use — guarded sections are rare (e.g. daily
        # rotation), so a long-lived connection isn't worth it.
        client: Redis = Redis.from_url(self._url)
        try:
            lock = client.lock(
                name,
                timeout=ttl.total_seconds(),
                blocking=False,
                raise_on_release_error=False,
            )
            try:
                acquired = await lock.acquire()
            except Exception:
                # A Redis outage must not leak the client or bubble into the
                # guarded loop: report "not acquired" and log at ERROR — every
                # replica skipping its guarded section (e.g. a day's rotation)
                # is an operational event worth alerting on.
                logger.exception(
                    "distributed lock %r unavailable; guarded section will be skipped", name
                )
                acquired = False
            try:
                yield acquired
            finally:
                if acquired:
                    await lock.release()
        finally:
            await client.aclose()


def build_distributed_lock(settings: Settings) -> DistributedLock:
    if settings.redis_url:
        return RedisDistributedLock(settings.redis_url)
    return NoOpDistributedLock()
