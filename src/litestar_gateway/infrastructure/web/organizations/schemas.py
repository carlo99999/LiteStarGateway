"""DTOs for organizations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities import Organization


@dataclass(frozen=True)
class CreateOrganizationRequest:
    name: str
    description: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UpdateOrganizationRequest:
    name: str
    description: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OrganizationResponse:
    id: UUID
    name: str
    created_at: datetime
    description: str | None
    tags: list[str]

    @classmethod
    def from_entity(cls, org: Organization) -> OrganizationResponse:
        return cls(
            id=org.id,
            name=org.name,
            created_at=org.created_at,
            description=org.description,
            tags=list(org.tags),
        )


@dataclass(frozen=True)
class TeamSpendResponse:
    team_id: UUID
    name: str
    cost: float


@dataclass(frozen=True)
class OrganizationSpendResponse:
    organization_id: UUID
    since: datetime
    total_cost: float
    teams: list[TeamSpendResponse]
