"""Admin endpoint to issue a single-use invite token (requires an admin JWT)."""

from __future__ import annotations

from litestar import post
from litestar.di import NamedDependency, Provide

from litestar_test.application.user_service import UserService
from litestar_test.domain.entities import User
from litestar_test.infrastructure.web.session.dependencies import provide_current_admin
from litestar_test.infrastructure.web.users.schemas import InviteResponse


@post("/invites", dependencies={"admin_user": Provide(provide_current_admin)})
async def create_invite(
    admin_user: NamedDependency[User], user_service: NamedDependency[UserService]
) -> InviteResponse:
    issued = await user_service.create_invite()
    return InviteResponse.from_issued(issued)
