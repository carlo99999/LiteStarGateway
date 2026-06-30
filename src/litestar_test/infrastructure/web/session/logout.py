"""Logout: invalidate the caller's existing JWTs (token_version bump)."""

from __future__ import annotations

from litestar import post
from litestar.di import NamedDependency, Provide
from litestar.status_codes import HTTP_204_NO_CONTENT

from litestar_test.application.user_service import UserService
from litestar_test.domain.entities import User
from litestar_test.infrastructure.web.session.dependencies import provide_current_user


@post(
    "/logout",
    summary="Log out (revoke existing tokens)",
    description="Invalidates all of the caller's previously issued JWTs.",
    status_code=HTTP_204_NO_CONTENT,
    dependencies={"current_user": Provide(provide_current_user)},
)
async def logout(
    current_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
) -> None:
    await user_service.logout(current_user)
