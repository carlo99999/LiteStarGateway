"""Port — audit trail."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from litestar_gateway.domain.entities import AuditEvent
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


@runtime_checkable
class AuditLog(Protocol):
    """Append-only audit trail of privileged actions."""

    async def stage(self, event: AuditEvent) -> None:
        """Stage an event in the caller-owned transaction."""
        ...

    async def record(self, event: AuditEvent) -> None: ...

    async def list_recent(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[AuditEvent]:
        """Most recent audit events first (for the admin read API)."""
        ...
