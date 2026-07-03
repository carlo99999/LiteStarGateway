"""DTOs for organizations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_test.domain.entities import Organization


@dataclass(frozen=True)
class CreateOrganizationRequest:
    name: str


@dataclass(frozen=True)
class OrganizationResponse:
    id: UUID
    name: str
    created_at: datetime

    @classmethod
    def from_entity(cls, org: Organization) -> OrganizationResponse:
        return cls(id=org.id, name=org.name, created_at=org.created_at)
