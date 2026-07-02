"""SQLAlchemy adapter implementing the `AuditLog` port.

Privileged actions are low-frequency (admin operations, logins), so the write is
synchronous and durable — an audit record must not be lost the way a best-effort
trace can be.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import AuditEvent
from litestar_test.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_test.infrastructure.persistence.orm import AuditEventModel


class SQLAlchemyAuditLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventModel(
                id=event.id,
                action=event.action,
                actor_id=event.actor_id,
                actor_email=event.actor_email,
                target_type=event.target_type,
                target_id=event.target_id,
                ip=event.ip,
                detail=event.detail,
            )
        )
        await self._session.commit()

    async def list_recent(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[AuditEvent]:
        rows = await self._session.scalars(
            select(AuditEventModel)
            .order_by(AuditEventModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [r.to_entity() for r in rows]
