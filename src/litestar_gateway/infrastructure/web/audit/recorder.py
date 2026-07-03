"""Helper to emit one privileged-action audit event from a web handler.

Called after the action succeeds — the handler has the actor (current user/admin),
the request (client IP), and the outcome. Keep `detail` free of secrets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar import Request

from litestar_gateway.domain.entities import AuditEvent, Principal, User
from litestar_gateway.domain.ports import AuditLog


async def record_audit(
    audit_log: AuditLog,
    request: Request,
    actor: User | Principal | None,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | UUID | None = None,
    detail: str | None = None,
) -> None:
    # A Principal may be a human or a team service principal (API key); the
    # trail records which one acted (email, or "api-key:<prefix>").
    if isinstance(actor, Principal):
        actor_id, actor_type = actor.audit_actor_id, actor.audit_actor_type
        actor_email = actor.audit_label
    else:
        actor_id, actor_email = (actor.id, actor.email) if actor else (None, None)
        actor_type = "user" if actor else None
    await audit_log.record(
        AuditEvent(
            id=uuid4(),
            action=action,
            actor_id=actor_id,
            actor_type=actor_type,
            actor_email=actor_email,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            ip=request.client.host if request.client else None,
            detail=detail,
            created_at=datetime.now(UTC),
        )
    )
