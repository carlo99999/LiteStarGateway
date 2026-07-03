"""Read API for the audit trail — platform-admin only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar import Controller, get
from litestar.di import NamedDependency, Provide
from litestar.params import FromQuery

from litestar_gateway.domain.entities import AuditEvent, User
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin


@dataclass(frozen=True)
class AuditEventResponse:
    id: UUID
    action: str
    actor_id: UUID | None
    actor_email: str | None
    target_type: str | None
    target_id: str | None
    ip: str | None
    detail: str | None
    created_at: datetime

    @classmethod
    def from_entity(cls, e: AuditEvent) -> AuditEventResponse:
        return cls(
            id=e.id,
            action=e.action,
            actor_id=e.actor_id,
            actor_email=e.actor_email,
            target_type=e.target_type,
            target_id=e.target_id,
            ip=e.ip,
            detail=e.detail,
            created_at=e.created_at,
        )


class AuditController(Controller):
    path = "/audit"
    tags = ["audit"]
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @get(summary="List recent audit events (most recent first)")
    async def list_audit(
        self,
        current_admin: NamedDependency[User],
        audit_log: NamedDependency[AuditLog],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[AuditEventResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        events = await audit_log.list_recent(limit=page_limit, offset=page_offset)
        return [AuditEventResponse.from_entity(e) for e in events]
