"""Admin-issued password reset.

A platform admin issues a single-use, expiring token for a user (like an invite);
the user redeems it and chooses their own new password, so the admin never sees
it. Redeeming revokes the user's existing sessions.
"""

from __future__ import annotations

from litestar import post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import ClientException
from litestar.status_codes import HTTP_204_NO_CONTENT

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.exceptions import InvalidPasswordReset, WeakPassword
from litestar_gateway.infrastructure.web.rate_limit import build_auth_rate_limit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin
from litestar_gateway.infrastructure.web.users.schemas import (
    PasswordResetCreateRequest,
    PasswordResetResponse,
    ResetPasswordRequest,
)


# Admin-gated, but rate-limited like the other auth-surface endpoints for consistency.
@post(
    "/password-resets",
    dependencies={"admin_user": Provide(provide_current_admin)},
    middleware=[build_auth_rate_limit().middleware],
)
async def create_password_reset(
    data: PasswordResetCreateRequest,
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
) -> PasswordResetResponse:
    issued = await user_service.create_password_reset(admin_user, data.email)
    return PasswordResetResponse.from_issued(issued)


@post(
    "/reset-password",
    status_code=HTTP_204_NO_CONTENT,
    middleware=[build_auth_rate_limit().middleware],
)
async def reset_password(
    data: ResetPasswordRequest, user_service: NamedDependency[UserService]
) -> None:
    try:
        await user_service.reset_password(
            reset_token=data.reset_token, new_password=data.new_password
        )
    except WeakPassword as exc:
        raise ClientException(str(exc)) from exc
    except InvalidPasswordReset as exc:
        # Generic, non-revealing: don't disclose whether the token existed.
        raise ClientException("Unable to reset password.") from exc
