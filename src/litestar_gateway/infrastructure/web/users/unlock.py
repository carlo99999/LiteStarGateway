"""Admin endpoint to lift a login lockout (requires an admin JWT).

The lockout escalates on consecutive cycles, so a sustained attacker can keep a
victim's password login locked for growing windows — this is the recovery lever:
a platform admin clears the lock (and the escalation) immediately.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Request, delete
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin


@delete("/users/{user_id:uuid}/lock", dependencies={"admin_user": Provide(provide_current_admin)})
async def unlock_user(
    request: Request,
    user_id: FromPath[UUID],
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    audit_log: NamedDependency[AuditLog],
) -> None:
    await user_service.unlock_user(admin_user, user_id)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "user.unlock",
        target_type="user",
        target_id=user_id,
    )
