"""Admin endpoint to list platform users (admin JWT), for the Users console."""

from __future__ import annotations

from litestar import get
from litestar.di import NamedDependency, Provide
from litestar.params import FromQuery

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin
from litestar_gateway.infrastructure.web.users.schemas import UserResponse


@get(
    "/users",
    dependencies={"admin_user": Provide(provide_current_admin)},
)
async def list_users(
    admin_user: NamedDependency[User],
    user_service: NamedDependency[UserService],
    limit: FromQuery[int | None] = None,
    offset: FromQuery[int | None] = None,
) -> list[UserResponse]:
    page_limit, page_offset = resolve_page(limit, offset)
    users = await user_service.list_users(admin_user, limit=page_limit, offset=page_offset)
    return [UserResponse.from_entity(u) for u in users]
