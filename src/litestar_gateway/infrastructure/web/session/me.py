"""Protected endpoint: returns the currently authenticated user."""

from __future__ import annotations

from litestar import get
from litestar.di import NamedDependency, Provide

from litestar_gateway.domain.entities import User
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user
from litestar_gateway.infrastructure.web.users.schemas import UserResponse


@get("/me", dependencies={"current_user": Provide(provide_current_user)})
async def me(current_user: NamedDependency[User]) -> UserResponse:
    return UserResponse.from_entity(current_user)
