"""DTOs for teams, memberships and team-scoped API keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from litestar_gateway.domain.entities import (
    APIKey,
    ApiKeyBudget,
    ApiKeySpend,
    AuditEvent,
    Budget,
    BudgetAlertState,
    IssuedKey,
    PendingBudgetAlert,
    ServicePrincipal,
    Team,
    TeamMembership,
    TeamRole,
    UsageAggregate,
    UsageBucket,
    UsageEvent,
    UsageTimeseries,
)
from litestar_gateway.domain.exceptions import InvalidKeyExpiry
from litestar_gateway.domain.money import ZERO


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
    # Per-team HMAC secret for the team's alert webhook. Omitting it keeps the
    # stored one — it is never returned, so an operator editing a threshold
    # cannot resubmit it. Removing it needs the explicit flag below: omission
    # cannot mean both "keep" and "go back to the platform-wide secret".
    alert_webhook_secret: str | None = None
    # `bool | None` rather than `bool = False`: a plain default still lands in the
    # OpenAPI `required` list, which tells every generated client that a field it
    # has never sent is mandatory. Absent and false mean the same thing here.
    clear_alert_webhook_secret: bool | None = None


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
    # Whether a per-team webhook secret is stored — never the value.
    has_alert_webhook_secret: bool = False

    @classmethod
    def from_budget(cls, budget: Budget, spent: Decimal) -> BudgetResponse:
        return cls(
            team_id=budget.team_id,
            limit_cost=float(budget.limit_cost),
            window=budget.window.value,
            spent=float(spent),
            remaining=float(max(ZERO, budget.limit_cost - spent)),
            thresholds=list(budget.thresholds),
            alert_webhook_url=budget.alert_webhook_url,
            alert_email=budget.alert_email,
            has_alert_webhook_secret=budget.has_alert_webhook_secret,
        )


@dataclass(frozen=True)
class SetKeyBudgetRequest:
    """A spend cap for one API key, inside the team's cap."""

    limit_cost: float  # USD, must be > 0
    window: str  # "monthly" | "daily"
    # `block` refuses the call once the cap is reached; `alert` lets it through
    # and records the overrun. Defaulting to `alert` is deliberate: adding
    # visibility to a key should not be able to break its owner's workload by
    # accident — breaking it has to be asked for.
    mode: str = "alert"


@dataclass(frozen=True)
class KeyBudgetResponse:
    api_key_id: UUID
    team_id: UUID
    limit_cost: float
    window: str
    mode: str
    spent: float  # this key's accumulated cost in the current window
    remaining: float  # never negative
    # True once spend has reached the cap. On an `alert`-mode budget this is the
    # whole signal — the call still went through, and this is what says so.
    over_limit: bool

    @classmethod
    def from_budget(cls, budget: ApiKeyBudget, spent: Decimal) -> KeyBudgetResponse:
        return cls(
            api_key_id=budget.api_key_id,
            team_id=budget.team_id,
            limit_cost=float(budget.limit_cost),
            window=budget.window.value,
            mode=budget.mode.value,
            spent=float(spent),
            remaining=float(max(ZERO, budget.limit_cost - spent)),
            over_limit=spent >= budget.limit_cost,
        )


@dataclass(frozen=True)
class PendingBudgetAlertResponse:
    """One undelivered alert still in the outbox — the quarantined view.

    `last_error` is carried because it is the only account of why delivery
    failed; `attempts` because it is what makes the row quarantined.
    """

    id: UUID
    team_id: UUID
    window: str
    threshold: int
    spend: float
    limit_cost: float
    attempts: int
    last_error: str | None
    created_at: datetime

    @classmethod
    def from_entity(cls, alert: PendingBudgetAlert) -> PendingBudgetAlertResponse:
        return cls(
            id=alert.id,
            team_id=alert.team_id,
            window=alert.window.value,
            threshold=alert.threshold,
            spend=float(alert.spend),
            limit_cost=float(alert.limit_cost),
            attempts=alert.attempts,
            last_error=alert.last_error,
            created_at=alert.created_at,
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
            cost=float(a.cost),
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
            cost=float(b.cost),
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
    # Tombstone timestamp (Plan 13 Phase 5); None for a live team. Only visible
    # through the export/purge admin actions — `get`/`list` hide the team
    # entirely once this is set, so it never appears here otherwise.
    deleted_at: datetime | None = None

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
            deleted_at=team.deleted_at,
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
            cost=float(spend.cost) if spend else 0.0,
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


@dataclass(frozen=True)
class TeamUsageEventResponse:
    """One raw usage_event row (export-before-delete, Plan 13 Phase 5) — the
    same fields the ledger records, unaggregated."""

    id: UUID
    team_id: UUID
    api_key_id: UUID | None
    model_id: UUID
    model_name: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    created_at: datetime
    requested_alias: str | None
    cache_hit: bool
    request_id: str | None

    @classmethod
    def from_entity(cls, e: UsageEvent) -> TeamUsageEventResponse:
        return cls(
            id=e.id,
            team_id=e.team_id,
            api_key_id=e.api_key_id,
            model_id=e.model_id,
            model_name=e.model_name,
            operation=e.operation,
            prompt_tokens=e.prompt_tokens,
            completion_tokens=e.completion_tokens,
            cost=float(e.cost),
            created_at=e.created_at,
            requested_alias=e.requested_alias,
            cache_hit=e.cache_hit,
            request_id=e.request_id,
        )


@dataclass(frozen=True)
class TeamAuditEventResponse:
    """One audit_event row targeting the team (export-before-delete)."""

    id: UUID
    action: str
    actor_id: UUID | None
    actor_type: str | None
    actor_email: str | None
    detail: str | None
    created_at: datetime

    @classmethod
    def from_entity(cls, e: AuditEvent) -> TeamAuditEventResponse:
        return cls(
            id=e.id,
            action=e.action,
            actor_id=e.actor_id,
            actor_type=e.actor_type,
            actor_email=e.actor_email,
            detail=e.detail,
            created_at=e.created_at,
        )


@dataclass(frozen=True)
class RoutingSavingsResponse:
    """Routing-decision aggregate for the team (see `export_team_data`'s
    docstring for why this is an aggregate, not a raw per-decision dump)."""

    total_estimated_savings: float
    decisions_counted: int
    decisions_without_usage: int


@dataclass(frozen=True)
class TeamExportResponse:
    """Full export-before-delete payload for one team (Plan 13 Phase 5):
    the team itself, its raw usage history, its audit trail, and a routing
    savings summary."""

    team: TeamResponse
    usage_events: list[TeamUsageEventResponse]
    audit_events: list[TeamAuditEventResponse]
    routing_savings: RoutingSavingsResponse | None

    @classmethod
    def from_export(cls, export: dict[str, Any]) -> TeamExportResponse:
        savings = export["routing_savings"]
        return cls(
            team=TeamResponse.from_entity(export["team"]),
            usage_events=[TeamUsageEventResponse.from_entity(e) for e in export["usage_events"]],
            audit_events=[TeamAuditEventResponse.from_entity(e) for e in export["audit_events"]],
            routing_savings=RoutingSavingsResponse(**savings) if savings is not None else None,
        )
