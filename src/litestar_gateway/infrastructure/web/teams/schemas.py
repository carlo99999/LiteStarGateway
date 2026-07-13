"""DTOs for teams, memberships and team-scoped API keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities import (
    APIKey,
    ApiKeySpend,
    Budget,
    IssuedKey,
    ServicePrincipal,
    Team,
    TeamMembership,
    TeamRole,
    UsageAggregate,
)


@dataclass(frozen=True)
class SetBudgetRequest:
    limit_cost: float  # USD, must be > 0
    window: str  # "monthly" | "daily"


@dataclass(frozen=True)
class BudgetResponse:
    team_id: UUID
    limit_cost: float
    window: str
    spent: float  # accumulated cost in the current window
    remaining: float  # never negative

    @classmethod
    def from_budget(cls, budget: Budget, spent: float) -> BudgetResponse:
        return cls(
            team_id=budget.team_id,
            limit_cost=budget.limit_cost,
            window=budget.window.value,
            spent=spent,
            remaining=max(0.0, budget.limit_cost - spent),
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
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    rate_limit_rpm: int | None = None  # requests/min for the team; None = unlimited


@dataclass(frozen=True)
class UpdateTeamRequest:
    name: str
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    rate_limit_rpm: int | None = None


@dataclass(frozen=True)
class TeamResponse:
    id: UUID
    organization_id: UUID
    name: str
    created_at: datetime
    description: str | None
    tags: list[str]
    rate_limit_rpm: int | None

    @classmethod
    def from_entity(cls, team: Team) -> TeamResponse:
        return cls(
            id=team.id,
            organization_id=team.organization_id,
            name=team.name,
            created_at=team.created_at,
            description=team.description,
            tags=list(team.tags),
            rate_limit_rpm=team.rate_limit_rpm,
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
    scope: str = "inference"  # inference | management | all
    rate_limit_rpm: int | None = None  # requests/min for this key; None = unlimited


@dataclass(frozen=True)
class CreatedKeyResponse:
    """Returned once at creation. `plaintext` is never retrievable again."""

    id: UUID
    team_id: UUID
    name: str | None
    prefix: str
    plaintext: str
    scope: str
    created_at: datetime
    rate_limit_rpm: int | None

    @classmethod
    def from_issued(cls, issued: IssuedKey) -> CreatedKeyResponse:
        k = issued.key
        return cls(
            id=k.id,
            team_id=k.team_id,
            name=k.name,
            prefix=k.prefix,
            plaintext=issued.plaintext,
            scope=k.scope.value,
            created_at=k.created_at,
            rate_limit_rpm=k.rate_limit_rpm,
        )


@dataclass(frozen=True)
class KeyResponse:
    id: UUID
    team_id: UUID
    created_by: UUID
    name: str | None
    prefix: str
    is_active: bool
    scope: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    rate_limit_rpm: int | None

    @classmethod
    def from_entity(cls, key) -> KeyResponse:  # noqa: ANN001 - APIKey entity
        return cls(
            id=key.id,
            team_id=key.team_id,
            created_by=key.created_by,
            name=key.name,
            prefix=key.prefix,
            is_active=key.is_active,
            scope=key.scope.value,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
            rate_limit_rpm=key.rate_limit_rpm,
        )


@dataclass(frozen=True)
class KeySpendingResponse:
    """An API key (active or revoked) with its accumulated spend.

    The identity block (name, prefix, is_active, created_at, revoked_at) is
    `None` for callers holding usage:read but not keys:read — `id` remains as
    an opaque correlation handle alongside the spend figures (R6-M43)."""

    id: UUID
    name: str | None
    prefix: str | None
    is_active: bool | None
    created_at: datetime | None
    revoked_at: datetime | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    calls: int

    @classmethod
    def from_key_and_spend(
        cls, key: APIKey, spend: ApiKeySpend | None, *, include_identity: bool = True
    ) -> KeySpendingResponse:
        prompt = spend.prompt_tokens if spend else 0
        completion = spend.completion_tokens if spend else 0
        return cls(
            id=key.id,
            name=key.name if include_identity else None,
            prefix=key.prefix if include_identity else None,
            is_active=key.is_active if include_identity else None,
            created_at=key.created_at if include_identity else None,
            revoked_at=key.revoked_at if include_identity else None,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost=spend.cost if spend else 0.0,
            calls=spend.calls if spend else 0,
        )


@dataclass(frozen=True)
class CreateServicePrincipalRequest:
    name: str


@dataclass(frozen=True)
class SetServicePrincipalEnabledRequest:
    enabled: bool


@dataclass(frozen=True)
class ServicePrincipalResponse:
    id: UUID
    team_id: UUID
    name: str
    enabled: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, sp: ServicePrincipal) -> ServicePrincipalResponse:
        return cls(
            id=sp.id,
            team_id=sp.team_id,
            name=sp.name,
            enabled=sp.enabled,
            created_at=sp.created_at,
        )
