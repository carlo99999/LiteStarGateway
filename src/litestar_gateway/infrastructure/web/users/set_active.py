"""Admin endpoint to enable/disable a user account (requires an admin JWT).

Disabling immediately locks the account out — its existing login JWTs are revoked
and new authentication is rejected — the lever an admin needs to shut out a
compromised or offboarded user without waiting for token expiry.
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
from litestar_gateway.infrastructure.web.users.schemas import SetUserActiveRequest, UserResponse


@patch("/users/{user_id:uuid}", dependencies={"admin_user": Provide(provide_current_admin)})
async def set_user_active(
    request: Request,
    user_id: FromPath[UUID],
    data: SetUserActiveRequest,
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    audit_log: NamedDependency[AuditLog],
) -> UserResponse:
    user = await user_service.set_user_active(admin_user, user_id, data.is_active)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "user.enable" if data.is_active else "user.disable",
        target_type="user",
        target_id=user_id,
    )
    return UserResponse.from_entity(user)
