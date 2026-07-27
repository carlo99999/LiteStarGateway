"""DTOs for teams, memberships and team-scoped API keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from litestar_gateway.domain.entities import (
    APIKey,
    ApiKeySpend,
    Budget,
    BudgetAlertState,
    IssuedKey,
    ServicePrincipal,
    Team,
    TeamMembership,
    TeamRole,
    UsageAggregate,
    UsageBucket,
    UsageTimeseries,
)
from litestar_gateway.domain.exceptions import InvalidKeyExpiry


@dataclass(frozen=True)
class SetBudgetRequest:
    limit_cost: float  # USD, must be > 0
    window: str  # "monthly" | "daily"
    # Alert config (Plan 07 Phase 3, design §8). All optional; omitted ⇒ no
    # thresholds and no channel targets. `thresholds` are percentages of the
    # cap (1..100), validated at the boundary. `alert_webhook_url` overrides
    # the platform webhook (SSRF-validated); `alert_email` opts the team into
    # email delivery via the platform SMTP server.
    thresholds: list[int] | None = None
    alert_webhook_url: str | None = None
    alert_email: str | None = None


@dataclass(frozen=True)
class BudgetResponse:
    team_id: UUID
    limit_cost: float
    window: str
    spent: float  # accumulated cost in the current window
    remaining: float  # never negative
    thresholds: list[int]
    alert_webhook_url: str | None
    alert_email: str | None

    @classmethod
    def from_budget(cls, budget: Budget, spent: float) -> BudgetResponse:
        return cls(
            team_id=budget.team_id,
            limit_cost=budget.limit_cost,
            window=budget.window.value,
            spent=spent,
            remaining=max(0.0, budget.limit_cost - spent),
            thresholds=list(budget.thresholds),
            alert_webhook_url=budget.alert_webhook_url,
            alert_email=budget.alert_email,
        )


@dataclass(frozen=True)
class BudgetAlertResponse:
    """One fired budget-threshold alert, for the console's recent-alerts list
    (Plan 07 Phase 3, design §8). Read from the `budget_alert_state` dedup
    ledger — the durable record of what fired and when."""

    team_id: UUID
    window: str
    period_start: datetime
    threshold: int
    fired_at: datetime

    @classmethod
    def from_entity(cls, alert: BudgetAlertState) -> BudgetAlertResponse:
        return cls(
            team_id=alert.team_id,
            window=alert.window.value,
            period_start=alert.period_start,
            threshold=alert.threshold,
            fired_at=alert.fired_at,
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
    row_id: str
    requested_alias: str
    resolved_model_id: UUID
    canonical_model_name: str
    callable_origin: str
    source_team_id: UUID | None

    @classmethod
    def from_aggregate(cls, a: UsageAggregate) -> UsageResponse:
        return cls(
            model=a.requested_alias or a.canonical_model_name or a.model_name,
            prompt_tokens=a.prompt_tokens,
            completion_tokens=a.completion_tokens,
            total_tokens=a.prompt_tokens + a.completion_tokens,
            cost=a.cost,
            calls=a.calls,
            row_id=(
                f"{a.resolved_model_id or a.model_id}:"
                f"{a.requested_alias or 'unknown'}:{a.callable_origin or 'unknown'}"
            ),
            requested_alias=a.requested_alias or "unknown",
            resolved_model_id=a.resolved_model_id or a.model_id,
            canonical_model_name=a.canonical_model_name or a.model_name,
            callable_origin=a.callable_origin or "unknown",
            source_team_id=a.source_team_id,
        )


@dataclass(frozen=True)
class UsageBucketResponse:
    """One bucket of `GET /teams/{id}/usage/timeseries` (Plan 10 Phase 1)."""

    bucket_start: datetime
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    # Non-None only when the request passed `group_by=model` (Plan 10 Phase 2):
    # the requested-alias-or-canonical-model-name label for this row.
    group_key: str | None = None

    @classmethod
    def from_bucket(cls, b: UsageBucket) -> UsageBucketResponse:
        return cls(
            bucket_start=b.bucket_start,
            request_count=b.request_count,
            prompt_tokens=b.prompt_tokens,
            completion_tokens=b.completion_tokens,
            total_tokens=b.prompt_tokens + b.completion_tokens,
            cost=b.cost,
            group_key=b.group_key,
        )


@dataclass(frozen=True)
class UsageTimeseriesResponse:
    """Bucketed usage over a bounded date range (Plan 10 Phase 1). Not
    paginated — the requested `[start, end)` range and granularity already
    bound the row count, so totals never depend on pagination."""

    team_id: UUID
    granularity: str
    start: datetime
    end: datetime
    buckets: list[UsageBucketResponse]

    @classmethod
    def from_timeseries(cls, series: UsageTimeseries) -> UsageTimeseriesResponse:
        return cls(
            team_id=series.team_id,
            granularity=series.granularity,
            start=series.start,
            end=series.end,
            buckets=[UsageBucketResponse.from_bucket(b) for b in series.buckets],
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
    # Optional TTL in days; the key stops authenticating after it. None/omitted
    # = no expiry. Must be a positive integer when given.
    expires_in_days: int | None = None


def resolve_key_expiry(expires_in_days: int | None) -> datetime | None:
    """Turn an optional TTL-in-days into an absolute expiry instant (or None).
    Rejects a non-positive TTL as a 400 (InvalidKeyExpiry)."""
    if expires_in_days is None:
        return None
    if expires_in_days <= 0:
        raise InvalidKeyExpiry("expires_in_days must be a positive integer")
    return datetime.now(UTC) + timedelta(days=expires_in_days)


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
    expires_at: datetime | None

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
            expires_at=k.expires_at,
        )


@dataclass(frozen=True)
class KeyResponse:
    id: UUID
    team_id: UUID
    created_by: UUID
    # Set when the key belongs to a service principal (a team identity); None for
    # a personal key attributed to the `created_by` user. Lets the console show
    # who a key acts as without a second lookup.
    service_principal_id: UUID | None
    name: str | None
    prefix: str
    is_active: bool
    scope: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    rate_limit_rpm: int | None
    expires_at: datetime | None

    @classmethod
    def from_entity(cls, key) -> KeyResponse:  # noqa: ANN001 - APIKey entity
        return cls(
            id=key.id,
            team_id=key.team_id,
            created_by=key.created_by,
            service_principal_id=key.service_principal_id,
            name=key.name,
            prefix=key.prefix,
            is_active=key.is_active,
            scope=key.scope.value,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
            rate_limit_rpm=key.rate_limit_rpm,
            expires_at=key.expires_at,
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
