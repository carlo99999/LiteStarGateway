"""DTOs for organizations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities import Organization


@dataclass(frozen=True)
class CreateOrganizationRequest:
    name: str


@dataclass(frozen=True)
class UpdateOrganizationRequest:
    name: str


@dataclass(frozen=True)
class OrganizationResponse:
    id: UUID
    name: str
    created_at: datetime

    @classmethod
    def from_entity(cls, org: Organization) -> OrganizationResponse:
        return cls(id=org.id, name=org.name, created_at=org.created_at)


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
