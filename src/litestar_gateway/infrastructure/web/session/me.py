"""Protected endpoints: the authenticated user and their team memberships."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from litestar import get
from litestar.di import NamedDependency, Provide

from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.entities import User
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user
from litestar_gateway.infrastructure.web.users.schemas import UserResponse


@get("/me", dependencies={"current_user": Provide(provide_current_user)})
async def me(current_user: NamedDependency[User]) -> UserResponse:
    return UserResponse.from_entity(current_user)


@dataclass(frozen=True)
class MyTeamResponse:
    """One of the caller's team memberships (self-scoped)."""

    team_id: UUID
    name: str
    role: str


@get("/me/teams", dependencies={"current_user": Provide(provide_current_user)})
async def my_teams(
    current_user: NamedDependency[User],
    team_service: NamedDependency[TeamService],
) -> list[MyTeamResponse]:
    """The caller's teams with their role — what a non-platform-admin sees on
    the dashboard."""
    teams = await team_service.list_user_teams(current_user)
    return [MyTeamResponse(team_id=t.id, name=t.name, role=role.value) for t, role in teams]
