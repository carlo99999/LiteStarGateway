"""Admin endpoint to grant/revoke another user's platform-admin role (admin JWT).

The platform role is binary (admin or member) and read live from the database on
every request, so a change here takes effect on the target's next call — no token
bump. This is the only way to *demote* an admin: SSO group / DEFAULT_ROLE sync is
upgrade-only and never downgrades a role.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Request, patch
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin
from litestar_gateway.infrastructure.web.users.schemas import SetUserAdminRequest, UserResponse


@patch(
    "/users/{user_id:uuid}/admin",
    dependencies={"admin_user": Provide(provide_current_admin)},
)
async def set_user_admin(
    request: Request,
    user_id: FromPath[UUID],
    data: SetUserAdminRequest,
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    audit_log: NamedDependency[AuditLog],
) -> UserResponse:
    user = await user_service.set_user_admin(admin_user, user_id, data.is_admin)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "user.grant_admin" if data.is_admin else "user.revoke_admin",
        target_type="user",
        target_id=user_id,
    )
    return UserResponse.from_entity(user)
