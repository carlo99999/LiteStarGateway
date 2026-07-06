"""Port — distributed lock."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Protocol


class DistributedLock(Protocol):
    """Cross-process mutual exclusion, e.g. so only one replica runs key rotation."""

    def hold(self, name: str, *, ttl: timedelta) -> AbstractAsyncContextManager[bool]:
        """Async context manager that yields True if the lock was acquired (held
        for the block, auto-expiring after `ttl`), or False if another holder has it."""
        ...
