"""Admin endpoint to issue a single-use invite token (requires an admin JWT)."""

from __future__ import annotations

from litestar import Request, post
from litestar.di import NamedDependency, Provide

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.rate_limit import build_auth_rate_limit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin
from litestar_gateway.infrastructure.web.users.schemas import InviteCreateRequest, InviteResponse


# Admin-gated, but rate-limited like the other auth-surface endpoints for consistency.
@post(
    "/invites",
    dependencies={"admin_user": Provide(provide_current_admin)},
    middleware=[build_auth_rate_limit().middleware],
)
async def create_invite(
    request: Request,
    data: InviteCreateRequest,
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    audit_log: NamedDependency[AuditLog],
) -> InviteResponse:
    issued = await user_service.create_invite(team_id=data.team_id, role=data.role)
    # Minting an invite creates an account-creation credential — audit who did it,
    # so account-takeover-via-admin-abuse is attributable (M35).
    await record_audit(
        audit_log,
        request,
        admin_user,
        "invite.create",
        target_type="invite",
        target_id=issued.invite.id,
    )
    return InviteResponse.from_issued(issued)
