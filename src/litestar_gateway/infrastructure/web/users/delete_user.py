"""Admin endpoint to hard-delete a user (admin JWT).

Self-deletion is refused with 403. Deleting another account is refused with 409
(UserHasReferences) if it still has team memberships or API keys it created —
deactivate instead in that case. A clean account is removed outright, along
with its pending password resets.
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


@delete(
    "/users/{user_id:uuid}",
    dependencies={"admin_user": Provide(provide_current_admin)},
)
async def delete_user(
    request: Request,
    user_id: FromPath[UUID],
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    audit_log: NamedDependency[AuditLog],
) -> None:
    user = await user_service.delete_user(admin_user, user_id)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "user.delete",
        target_type="user",
        target_id=user.id,
        detail=user.email,
    )
