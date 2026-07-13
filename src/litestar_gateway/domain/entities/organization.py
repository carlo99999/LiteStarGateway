"""Organization, team, and membership entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from .enums import TeamRole


@dataclass(frozen=True)
class Organization:
    """A container of teams. Has no direct users."""

    id: UUID
    name: str
    created_at: datetime
    description: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Team:
    """A team belongs to exactly one organization."""

    id: UUID
    organization_id: UUID
    name: str
    created_at: datetime
    description: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TeamMembership:
    """A user's membership in a team, with a role."""

    id: UUID
    team_id: UUID
    user_id: UUID
    role: TeamRole
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role is TeamRole.ADMIN
