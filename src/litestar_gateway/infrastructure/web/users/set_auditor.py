"""Admin endpoint to grant/revoke the platform-auditor role (admin JWT).

The auditor role is read-only — audit log plus every team's usage/budget
figures — and, like the admin flag, is read live from the database on every
request, so a change takes effect on the target's next call.
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
from litestar_gateway.infrastructure.web.users.schemas import SetUserAuditorRequest, UserResponse


@patch(
    "/users/{user_id:uuid}/auditor",
    dependencies={"admin_user": Provide(provide_current_admin)},
)
async def set_user_auditor(
    request: Request,
    user_id: FromPath[UUID],
    data: SetUserAuditorRequest,
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    audit_log: NamedDependency[AuditLog],
) -> UserResponse:
    user = await user_service.set_user_auditor(admin_user, user_id, data.is_auditor)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "user.grant_auditor" if data.is_auditor else "user.revoke_auditor",
        target_type="user",
        target_id=user_id,
    )
    return UserResponse.from_entity(user)
