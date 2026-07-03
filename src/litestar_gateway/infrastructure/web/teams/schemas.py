"""DTOs for teams, memberships and team-scoped API keys."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_test.domain.entities import (
    APIKey,
    ApiKeySpend,
    IssuedKey,
    Team,
    TeamMembership,
    TeamRole,
    UsageAggregate,
)


@dataclass(frozen=True)
class UsageResponse:
    """Per-model usage totals (over the requested api-key/model filter)."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    calls: int

    @classmethod
    def from_aggregate(cls, a: UsageAggregate) -> UsageResponse:
        return cls(
            model=a.model_name,
            prompt_tokens=a.prompt_tokens,
            completion_tokens=a.completion_tokens,
            total_tokens=a.prompt_tokens + a.completion_tokens,
            cost=a.cost,
            calls=a.calls,
        )


@dataclass(frozen=True)
class CreateTeamRequest:
    name: str
    admin_email: str  # first team-admin (must be an existing user)


@dataclass(frozen=True)
class TeamResponse:
    id: UUID
    organization_id: UUID
    name: str
    created_at: datetime

    @classmethod
    def from_entity(cls, team: Team) -> TeamResponse:
        return cls(
            id=team.id,
            organization_id=team.organization_id,
            name=team.name,
            created_at=team.created_at,
        )


@dataclass(frozen=True)
class AddMemberRequest:
    email: str
    role: TeamRole = TeamRole.MEMBER


@dataclass(frozen=True)
class SetRoleRequest:
    role: TeamRole


@dataclass(frozen=True)
class MembershipResponse:
    id: UUID
    team_id: UUID
    user_id: UUID
    role: TeamRole
    created_at: datetime

    @classmethod
    def from_entity(cls, m: TeamMembership) -> MembershipResponse:
        return cls(
            id=m.id,
            team_id=m.team_id,
            user_id=m.user_id,
            role=m.role,
            created_at=m.created_at,
        )


@dataclass(frozen=True)
class CreateKeyRequest:
    name: str | None = None


@dataclass(frozen=True)
class CreatedKeyResponse:
    """Returned once at creation. `plaintext` is never retrievable again."""

    id: UUID
    team_id: UUID
    name: str | None
    prefix: str
    plaintext: str
    created_at: datetime

    @classmethod
    def from_issued(cls, issued: IssuedKey) -> CreatedKeyResponse:
        k = issued.key
        return cls(
            id=k.id,
            team_id=k.team_id,
            name=k.name,
            prefix=k.prefix,
            plaintext=issued.plaintext,
            created_at=k.created_at,
        )


@dataclass(frozen=True)
class KeyResponse:
    id: UUID
    team_id: UUID
    created_by: UUID
    name: str | None
    prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_entity(cls, key) -> KeyResponse:  # noqa: ANN001 - APIKey entity
        return cls(
            id=key.id,
            team_id=key.team_id,
            created_by=key.created_by,
            name=key.name,
            prefix=key.prefix,
            is_active=key.is_active,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
        )


@dataclass(frozen=True)
class KeySpendingResponse:
    """An API key (active or revoked) with its accumulated spend."""

    id: UUID
    name: str | None
    prefix: str
    is_active: bool
    created_at: datetime
    revoked_at: datetime | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    calls: int

    @classmethod
    def from_key_and_spend(cls, key: APIKey, spend: ApiKeySpend | None) -> KeySpendingResponse:
        prompt = spend.prompt_tokens if spend else 0
        completion = spend.completion_tokens if spend else 0
        return cls(
            id=key.id,
            name=key.name,
            prefix=key.prefix,
            is_active=key.is_active,
            created_at=key.created_at,
            revoked_at=key.revoked_at,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost=spend.cost if spend else 0.0,
            calls=spend.calls if spend else 0,
        )
