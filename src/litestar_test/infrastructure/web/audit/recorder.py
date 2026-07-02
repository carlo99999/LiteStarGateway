"""Helper to emit one privileged-action audit event from a web handler.

Called after the action succeeds — the handler has the actor (current user/admin),
the request (client IP), and the outcome. Keep `detail` free of secrets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar import Request

from litestar_test.domain.entities import AuditEvent, User
from litestar_test.domain.ports import AuditLog


async def record_audit(
    audit_log: AuditLog,
    request: Request,
    actor: User | None,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | UUID | None = None,
    detail: str | None = None,
) -> None:
    await audit_log.record(
        AuditEvent(
            id=uuid4(),
            action=action,
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            ip=request.client.host if request.client else None,
            detail=detail,
            created_at=datetime.now(UTC),
        )
    )
