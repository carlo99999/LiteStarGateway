"""SQLAlchemy ORM mappings (persistence detail)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from advanced_alchemy.extensions.litestar import base
from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from litestar_gateway.domain.callable_alias import CallableOrigin
from litestar_gateway.domain.entities import (
    APIKey,
    ApiKeyBudget,
    AuditEvent,
    Budget,
    BudgetAlertState,
    BudgetWindow,
    Credential,
    GuardrailKind,
    GuardrailRule,
    Invite,
    KeyBudgetMode,
    KeyPurpose,
    KeyScope,
    Model,
    ModelGrant,
    ModelType,
    Organization,
    PasswordReset,
    PendingBudgetAlert,
    Provider,
    ScimToken,
    SecretKey,
    ServicePrincipal,
    SsoSettings,
    Team,
    TeamMembership,
    TeamRole,
    UsageEvent,
    User,
    parse_team_mapping,
)
from litestar_gateway.domain.entities.model import DEFAULT_CAPABILITIES
from litestar_gateway.domain.guardrails import Direction, FailPolicy
from litestar_gateway.domain.mcp import (
    ApiKeyToolPolicy,
    McpServer,
    McpServerGrant,
    McpTool,
    ToolEffect,
)
from litestar_gateway.domain.money import ZERO
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RouterGrant,
    RoutingDecisionRecord,
)
from litestar_gateway.infrastructure.persistence.money_type import Money


class UserModel(base.UUIDAuditBase):
    __tablename__ = "user_account"

    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str] = mapped_column()
    is_admin: Mapped[bool] = mapped_column(default=False)
    token_version: Mapped[int] = mapped_column(default=0)
    # The IdP subject this account is federated to (NULL for password-only
    # accounts). Unique so two identities can't bind to the same local account.
    sso_subject: Mapped[str | None] = mapped_column(default=None, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    # Which lever disabled the account ("admin" or "scim"); NULL while active.
    deactivated_by: Mapped[str | None] = mapped_column(default=None)
    # The IdP's SCIM externalId, once SCIM-provisioned/adopted. Unique so two IdP
    # records can't bind to the same local account.
    external_id: Mapped[str | None] = mapped_column(default=None, unique=True, index=True)
    # Read-only platform auditor (audit log + every team's usage/budget).
    is_auditor: Mapped[bool] = mapped_column(default=False)
    failed_login_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    lockout_cycles: Mapped[int] = mapped_column(default=0)

    def to_entity(self) -> User:
        return User(
            id=self.id,
            email=self.email,
            password_hash=self.password_hash,
            is_admin=self.is_admin,
            created_at=self.created_at,
            token_version=self.token_version,
            sso_subject=self.sso_subject,
            is_active=self.is_active,
            deactivated_by=self.deactivated_by,
            external_id=self.external_id,
            is_auditor=self.is_auditor,
            failed_login_attempts=self.failed_login_attempts,
            locked_until=self.locked_until,
            lockout_cycles=self.lockout_cycles,
        )


class AuditEventModel(base.UUIDAuditBase):
    __tablename__ = "audit_event"

    action: Mapped[str] = mapped_column(index=True)
    actor_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
    actor_type: Mapped[str | None] = mapped_column(default=None)
    actor_email: Mapped[str | None] = mapped_column(default=None)
    target_type: Mapped[str | None] = mapped_column(default=None)
    target_id: Mapped[str | None] = mapped_column(default=None, index=True)
    ip: Mapped[str | None] = mapped_column(default=None)
    detail: Mapped[str | None] = mapped_column(default=None)
    # Request correlation id (Plan 11 Slice A). Nullable: historical rows and
    # background-worker-originated events genuinely have no request to tag.
    request_id: Mapped[str | None] = mapped_column(default=None)

    def to_entity(self) -> AuditEvent:
        return AuditEvent(
            id=self.id,
            action=self.action,
            actor_id=self.actor_id,
            actor_type=self.actor_type,
            actor_email=self.actor_email,
            target_type=self.target_type,
            target_id=self.target_id,
            ip=self.ip,
            detail=self.detail,
            created_at=self.created_at,
            request_id=self.request_id,
        )


class InviteModel(base.UUIDAuditBase):
    __tablename__ = "invite"

    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    team_id: Mapped[UUID | None] = mapped_column(ForeignKey("team.id"), default=None, index=True)
    role: Mapped[str | None] = mapped_column(default=None)

    def to_entity(self) -> Invite:
        return Invite(
            id=self.id,
            token_hash=self.token_hash,
            created_at=self.created_at,
            expires_at=self.expires_at,
            used_at=self.used_at,
            team_id=self.team_id,
            role=self.role,
        )


class PasswordResetModel(base.UUIDAuditBase):
    __tablename__ = "password_reset"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.id"), index=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> PasswordReset:
        return PasswordReset(
            id=self.id,
            user_id=self.user_id,
            token_hash=self.token_hash,
            created_at=self.created_at,
            expires_at=self.expires_at,
            used_at=self.used_at,
        )


class ScimTokenModel(base.UUIDAuditBase):
    __tablename__ = "scim_token"

    name: Mapped[str] = mapped_column()
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> ScimToken:
        return ScimToken(
            id=self.id,
            name=self.name,
            token_hash=self.token_hash,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
        )


class RouterModel(base.UUIDAuditBase):
    __tablename__ = "router"
    __table_args__ = (
        UniqueConstraint("team_id", "name"),
        Index(
            "uq_global_router_name",
            "name",
            unique=True,
            sqlite_where=text("team_id IS NULL"),
            postgresql_where=text("team_id IS NULL"),
        ),
    )

    # NULL ⇒ a global (platform) router, callable by every team.
    team_id: Mapped[UUID | None] = mapped_column(ForeignKey("team.id"), index=True, default=None)
    name: Mapped[str] = mapped_column(index=True)
    # Candidate profiles as declared by the admin (see domain/routing.py).
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    default_model: Mapped[str] = mapped_column()
    strategy: Mapped[str] = mapped_column()
    strategy_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    shadow_strategy: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    # The originally-owning team, kept when a router is promoted to global.
    origin_team_id: Mapped[UUID | None] = mapped_column(default=None)
    # Pointer to the immutable snapshot used by direct/global callers. Kept
    # nullable so router identity and its first revision can be inserted in one
    # transaction without a circular FK dependency.
    current_revision_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
    # Cross-provider failover (Plan 05). Off by default: existing routers are
    # unaffected until an admin opts in explicitly.
    failover_enabled: Mapped[bool] = mapped_column(default=False)
    max_attempts: Mapped[int] = mapped_column(default=3)
    overall_deadline_ms: Mapped[int | None] = mapped_column(default=None)

    def to_entity(self) -> RouterConfig:
        return RouterConfig(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            candidates=tuple(
                CandidateModel(
                    model_name=c["model_name"],
                    description=c.get("description", ""),
                    quality_tier=QualityTier(c["quality_tier"]),
                    supports_vision=c.get("supports_vision", False),
                    supports_tools=c.get("supports_tools", False),
                    supports_json_schema=c.get("supports_json_schema", False),
                    context_window_tokens=c.get("context_window_tokens"),
                    input_cost_per_token=c.get("input_cost_per_token"),
                    output_cost_per_token=c.get("output_cost_per_token"),
                    weight=c.get("weight"),
                )
                for c in self.candidates
            ),
            default_model=self.default_model,
            strategy=self.strategy,
            strategy_config=self.strategy_config,
            enabled=self.enabled,
            created_at=self.created_at,
            shadow_strategy=self.shadow_strategy,
            origin_team_id=self.origin_team_id,
            failover_enabled=self.failover_enabled,
            max_attempts=self.max_attempts,
            overall_deadline_ms=self.overall_deadline_ms,
        )


class RouterRevisionModel(base.UUIDAuditBase):
    """Append-only router configuration snapshot."""

    __tablename__ = "router_revision"
    __table_args__ = (UniqueConstraint("router_id", "revision_number"),)

    router_id: Mapped[UUID] = mapped_column(ForeignKey("router.id", ondelete="CASCADE"), index=True)
    revision_number: Mapped[int] = mapped_column()
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    default_model_id: Mapped[UUID] = mapped_column()
    default_model_name: Mapped[str] = mapped_column()
    strategy: Mapped[str] = mapped_column()
    strategy_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    shadow_strategy: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    # Cross-provider failover (Plan 05). Off by default: existing revisions are
    # unaffected until an admin opts in explicitly.
    failover_enabled: Mapped[bool] = mapped_column(default=False)
    max_attempts: Mapped[int] = mapped_column(default=3)
    overall_deadline_ms: Mapped[int | None] = mapped_column(default=None)

    def to_entity(
        self,
        router: RouterModel,
        *,
        grant_id: UUID | None = None,
        ack_active_prompt_egress: bool = False,
        ack_shadow_prompt_egress: bool = False,
    ) -> RouterConfig:
        return RouterConfig(
            id=router.id,
            team_id=router.team_id,
            name=router.name,
            candidates=tuple(
                CandidateModel(
                    model_name=c["model_name"],
                    description=c.get("description", ""),
                    quality_tier=QualityTier(c["quality_tier"]),
                    model_id=UUID(str(c["model_id"])),
                    model_origin=c.get("model_origin"),
                    source_team_id=(
                        UUID(str(c["source_team_id"])) if c.get("source_team_id") else None
                    ),
                    supports_vision=c.get("supports_vision", False),
                    supports_tools=c.get("supports_tools", False),
                    supports_json_schema=c.get("supports_json_schema", False),
                    context_window_tokens=c.get("context_window_tokens"),
                    input_cost_per_token=c.get("input_cost_per_token"),
                    output_cost_per_token=c.get("output_cost_per_token"),
                    weight=c.get("weight"),
                )
                for c in self.candidates
            ),
            default_model=self.default_model_name,
            strategy=self.strategy,
            strategy_config=self.strategy_config,
            enabled=self.enabled,
            created_at=router.created_at,
            shadow_strategy=self.shadow_strategy,
            origin_team_id=router.origin_team_id,
            revision_id=self.id,
            revision_number=self.revision_number,
            default_model_id=self.default_model_id,
            grant_id=grant_id,
            ack_active_prompt_egress=ack_active_prompt_egress,
            ack_shadow_prompt_egress=ack_shadow_prompt_egress,
            failover_enabled=self.failover_enabled,
            max_attempts=self.max_attempts,
            overall_deadline_ms=self.overall_deadline_ms,
        )


class RouterGrantModel(base.UUIDAuditBase):
    """A team-owned router extended to another team, under `alias`."""

    __tablename__ = "router_grant"
    __table_args__ = (
        UniqueConstraint("team_id", "alias"),
        UniqueConstraint("router_id", "team_id"),
    )

    # A source router with an approved grant cannot be deleted implicitly.
    router_id: Mapped[UUID] = mapped_column(ForeignKey("router.id"), index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    alias: Mapped[str] = mapped_column()
    revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("router_revision.id"), index=True, default=None
    )
    ack_active_prompt_egress: Mapped[bool] = mapped_column(default=False)
    ack_shadow_prompt_egress: Mapped[bool] = mapped_column(default=False)

    def to_entity(self, revision_number: int | None = None) -> RouterGrant:
        return RouterGrant(
            id=self.id,
            router_id=self.router_id,
            team_id=self.team_id,
            alias=self.alias,
            created_at=self.created_at,
            revision_id=self.revision_id,
            revision_number=revision_number,
            ack_active_prompt_egress=self.ack_active_prompt_egress,
            ack_shadow_prompt_egress=self.ack_shadow_prompt_egress,
        )


class RoutingDecisionModel(base.UUIDAuditBase):
    __tablename__ = "routing_decision"
    __table_args__ = (
        Index(
            "ix_routing_decision_team_id_router_name_created_at",
            "team_id",
            "router_name",
            "created_at",
        ),
        # Per-router reads filter by (team_id, router_id): stable across renames,
        # immune to name reuse. No FK to `router` on purpose — decision history
        # must survive router deletion.
        Index(
            "ix_routing_decision_team_id_router_id_created_at",
            "team_id",
            "router_id",
            "created_at",
        ),
    )

    team_id: Mapped[UUID] = mapped_column()
    router_id: Mapped[UUID | None] = mapped_column(default=None)
    router_name: Mapped[str] = mapped_column()
    strategy: Mapped[str] = mapped_column()
    chosen_model: Mapped[str] = mapped_column()
    tier: Mapped[str | None] = mapped_column(default=None)
    score: Mapped[float | None] = mapped_column(default=None)
    signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    decision_ms: Mapped[float] = mapped_column(default=0.0)
    is_shadow: Mapped[bool] = mapped_column(default=False)
    fallback_used: Mapped[bool] = mapped_column(default=False)
    api_key_id: Mapped[UUID | None] = mapped_column(default=None)
    # Per-token RATES, not amounts: quantizing these at cost scale would round
    # a 0.0000005 rate to 0.000001 and corrupt the savings arithmetic. They move
    # to the rate scale together with the model's own rate columns.
    chosen_input_cost: Mapped[float | None] = mapped_column(default=None)
    chosen_output_cost: Mapped[float | None] = mapped_column(default=None)
    alt_input_cost: Mapped[float | None] = mapped_column(default=None)
    alt_output_cost: Mapped[float | None] = mapped_column(default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(default=None)
    completion_tokens: Mapped[int | None] = mapped_column(default=None)
    user_text: Mapped[str | None] = mapped_column(default=None)
    system_prompt: Mapped[str | None] = mapped_column(default=None)
    router_revision_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
    chosen_model_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
    # Cross-provider failover observability (Plan 05 Phase 3).
    attempts: Mapped[int] = mapped_column(default=1)
    failover_used: Mapped[bool] = mapped_column(default=False)
    # Request correlation id (Plan 11 Slice A). Nullable: rows written before
    # the column existed, and shadow/background decisions outside a request.
    request_id: Mapped[str | None] = mapped_column(default=None)

    def to_entity(self) -> RoutingDecisionRecord:
        return RoutingDecisionRecord(
            id=self.id,
            team_id=self.team_id,
            router_id=self.router_id,
            router_name=self.router_name,
            strategy=self.strategy,
            chosen_model=self.chosen_model,
            tier=self.tier,
            score=self.score,
            signals=tuple(self.signals),
            decision_ms=self.decision_ms,
            is_shadow=self.is_shadow,
            fallback_used=self.fallback_used,
            api_key_id=self.api_key_id,
            created_at=self.created_at,
            chosen_input_cost=self.chosen_input_cost,
            chosen_output_cost=self.chosen_output_cost,
            alt_input_cost=self.alt_input_cost,
            alt_output_cost=self.alt_output_cost,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            user_text=self.user_text,
            system_prompt=self.system_prompt,
            router_revision_id=self.router_revision_id,
            chosen_model_id=self.chosen_model_id,
            attempts=self.attempts,
            failover_used=self.failover_used,
            request_id=self.request_id,
        )


class OrganizationModel(base.UUIDAuditBase):
    __tablename__ = "organization"

    name: Mapped[str] = mapped_column(index=True)
    description: Mapped[str | None] = mapped_column(default=None)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    def to_entity(self) -> Organization:
        return Organization(
            id=self.id,
            name=self.name,
            created_at=self.created_at,
            description=self.description,
            tags=list(self.tags or []),
        )


class TeamModel(base.UUIDAuditBase):
    __tablename__ = "team"

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(index=True)
    description: Mapped[str | None] = mapped_column(default=None)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    rate_limit_rpm: Mapped[int | None] = mapped_column(default=None)
    # Tombstone (Plan 13 Phase 5). NULL = live team. Set instead of a hard
    # delete when the team has billed history; indexed so "hide soft-deleted
    # teams" filters stay cheap on the list/lookup paths.
    deleted_at: Mapped[datetime | None] = mapped_column(default=None, index=True)

    def to_entity(self) -> Team:
        return Team(
            id=self.id,
            organization_id=self.organization_id,
            name=self.name,
            created_at=self.created_at,
            description=self.description,
            tags=list(self.tags or []),
            rate_limit_rpm=self.rate_limit_rpm,
            deleted_at=self.deleted_at,
        )


class TeamMembershipModel(base.UUIDAuditBase):
    __tablename__ = "team_membership"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("user_account.id"), index=True)
    role: Mapped[str] = mapped_column()

    def to_entity(self) -> TeamMembership:
        return TeamMembership(
            id=self.id,
            team_id=self.team_id,
            user_id=self.user_id,
            role=TeamRole(self.role),
            created_at=self.created_at,
        )


class UsageEventModel(base.UUIDAuditBase):
    __tablename__ = "usage_event"
    # Covers the budget gate's hot-path read (team_id + created_at range SUM).
    __table_args__ = (Index("ix_usage_event_team_id_created_at", "team_id", "created_at"),)

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    api_key_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("api_key.id"), index=True, nullable=True
    )
    model_id: Mapped[UUID] = mapped_column(index=True)
    model_name: Mapped[str] = mapped_column()
    requested_alias: Mapped[str | None] = mapped_column(default=None, index=True)
    resolved_model_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
    canonical_model_name: Mapped[str | None] = mapped_column(default=None, index=True)
    callable_origin: Mapped[str | None] = mapped_column(default=None)
    source_team_id: Mapped[UUID | None] = mapped_column(default=None)
    operation: Mapped[str] = mapped_column()
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    cache_hit: Mapped[bool] = mapped_column(default=False)
    # Non-token billing dimensions (Plan 13 Phase 1): Anthropic prompt-cache
    # write/read token counts and the number of generated images.
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    image_count: Mapped[int] = mapped_column(default=0)
    # Request correlation id (Plan 11 Slice A). Nullable: historical rows and
    # reconciler-drained rows may have none (see PendingUsageEventModel).
    request_id: Mapped[str | None] = mapped_column(default=None)

    def to_entity(self) -> UsageEvent:
        return UsageEvent(
            id=self.id,
            team_id=self.team_id,
            api_key_id=self.api_key_id,
            model_id=self.model_id,
            model_name=self.model_name,
            operation=self.operation,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost=self.cost,
            created_at=self.created_at,
            requested_alias=self.requested_alias,
            resolved_model_id=self.resolved_model_id,
            canonical_model_name=self.canonical_model_name,
            callable_origin=self.callable_origin,
            source_team_id=self.source_team_id,
            cache_hit=self.cache_hit,
            cache_write_tokens=self.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens,
            image_count=self.image_count,
            request_id=self.request_id,
        )


class PendingUsageEventModel(base.UUIDAuditBase):
    """Dead-letter outbox for usage events whose ledger write failed. A background
    reconciler retries these into `usage_event` (idempotent by `event_id`), so a
    transient failure never silently loses a billing record.

    Not a write-ahead log: rows are written only after a ledger write has
    failed, so a crash before either write still loses the event (at-most-once
    on crash — see CompletionService._record_usage)."""

    __tablename__ = "pending_usage_event"

    event_id: Mapped[UUID] = mapped_column(unique=True, index=True)  # intended usage_event.id
    team_id: Mapped[UUID] = mapped_column(index=True)
    api_key_id: Mapped[UUID | None] = mapped_column(nullable=True)
    model_id: Mapped[UUID] = mapped_column()
    model_name: Mapped[str] = mapped_column()
    requested_alias: Mapped[str | None] = mapped_column(default=None)
    resolved_model_id: Mapped[UUID | None] = mapped_column(default=None)
    canonical_model_name: Mapped[str | None] = mapped_column(default=None)
    callable_origin: Mapped[str | None] = mapped_column(default=None)
    source_team_id: Mapped[UUID | None] = mapped_column(default=None)
    operation: Mapped[str] = mapped_column()
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    cache_hit: Mapped[bool] = mapped_column(default=False)
    # Non-token billing dimensions (Plan 13 Phase 1) — carried through to the
    # ledger row when the reconciler drains this dead-letter entry.
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    image_count: Mapped[int] = mapped_column(default=0)
    # Request correlation id (Plan 11 Slice A) — carried through to the
    # ledger row when the reconciler drains this dead-letter entry.
    request_id: Mapped[str | None] = mapped_column(default=None)
    event_created_at: Mapped[datetime] = mapped_column()
    # Poison-message bookkeeping: rows that keep failing the ledger insert are
    # quarantined after MAX_RECONCILE_ATTEMPTS instead of starving the batch.
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(default=None)


class TeamBudgetModel(base.UUIDAuditBase):
    """A team's hard spend cap (at most one row per team)."""

    __tablename__ = "team_budget"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), unique=True, index=True)
    limit_cost: Mapped[Decimal] = mapped_column(Money)
    window: Mapped[str] = mapped_column()
    # Alert threshold percentages of limit_cost (Plan 07 Phase 0, design §2/§8),
    # e.g. [50, 80, 100]. server_default backfills existing rows for the
    # NOT NULL add (migration f358c2474285's successor); unused by any request
    # path until Plan 07 Phase 1.
    thresholds: Mapped[list[int]] = mapped_column(JSON, default=list)
    # Optional per-team alert delivery targets (Plan 07 Phase 3, design §8).
    # Nullable: a team may configure a webhook, an email, both, or neither.
    # `alert_webhook_url` overrides the platform-wide webhook; `alert_email`
    # is delivered via the platform-wide SMTP server.
    alert_webhook_url: Mapped[str | None] = mapped_column(default=None)
    alert_email: Mapped[str | None] = mapped_column(default=None)
    # Per-team HMAC secret for `alert_webhook_url`, envelope-encrypted like every
    # other stored secret. NULL means "sign with the platform-wide secret": a
    # receiver that only ever sees this gateway needs no per-team key, and one
    # that hosts several tenants' endpoints does.
    encrypted_alert_webhook_secret: Mapped[str | None] = mapped_column(default=None)
    alert_webhook_secret_key_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("secret_key.id"), default=None
    )

    def to_entity(self) -> Budget:
        return Budget(
            id=self.id,
            team_id=self.team_id,
            limit_cost=self.limit_cost,
            window=BudgetWindow(self.window),
            created_at=self.created_at,
            thresholds=list(self.thresholds or []),
            alert_webhook_url=self.alert_webhook_url,
            alert_email=self.alert_email,
            has_alert_webhook_secret=self.encrypted_alert_webhook_secret is not None,
        )


class BudgetAlertStateModel(base.UUIDAuditBase):
    """Dedup ledger for fired budget-threshold alerts (Plan 07 Phase 0). One
    row per `(team_id, window, period_start, threshold)` that has fired; the
    unique constraint is the concurrency guard — a losing concurrent insert
    for the same key hits it and is treated as a no-op by the repository."""

    __tablename__ = "budget_alert_state"
    __table_args__ = (UniqueConstraint("team_id", "window", "period_start", "threshold"),)

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    window: Mapped[str] = mapped_column()
    period_start: Mapped[datetime] = mapped_column()
    threshold: Mapped[int] = mapped_column()
    fired_at: Mapped[datetime] = mapped_column()

    def to_entity(self) -> BudgetAlertState:
        return BudgetAlertState(
            id=self.id,
            team_id=self.team_id,
            window=BudgetWindow(self.window),
            period_start=self.period_start,
            threshold=self.threshold,
            fired_at=self.fired_at,
        )


class PendingBudgetAlertModel(base.UUIDAuditBase):
    """Durable outbox for newly-fired budget-threshold alerts (Plan 07 Phase 1).
    Mirrors `PendingUsageEventModel`'s shape (including the poison-quarantine
    `attempts`/`last_error` columns) so Phase 2's delivery worker can reuse the
    same drain/retry/quarantine pattern once `NotificationChannel` exists —
    nothing reads or drains this table yet."""

    __tablename__ = "pending_budget_alert"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    window: Mapped[str] = mapped_column()
    period_start: Mapped[datetime] = mapped_column()
    threshold: Mapped[int] = mapped_column()
    spend: Mapped[Decimal] = mapped_column(Money)
    limit_cost: Mapped[Decimal] = mapped_column(Money)
    # Poison-message bookkeeping, unused until Phase 2's delivery worker exists —
    # reserved now so that phase doesn't need another migration.
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(default=None)
    # Dispatch lease (ISSUE-026): set by the dispatcher that owns this row, so
    # two replicas cannot both deliver it. A lease in the past is claimable
    # again, which is what makes a worker dying mid-delivery recoverable
    # without operator action.
    claimed_until: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> PendingBudgetAlert:
        return PendingBudgetAlert(
            id=self.id,
            team_id=self.team_id,
            window=BudgetWindow(self.window),
            period_start=self.period_start,
            threshold=self.threshold,
            attempts=self.attempts,
            last_error=self.last_error,
            spend=self.spend,
            limit_cost=self.limit_cost,
            created_at=self.created_at,
        )


class SecretKeyModel(base.UUIDAuditBase):
    __tablename__ = "secret_key"

    purpose: Mapped[str] = mapped_column(index=True)
    # Master-wrapped key material (never stored in the clear).
    material: Mapped[str] = mapped_column()
    retired_at: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> SecretKey:
        return SecretKey(
            id=self.id,
            purpose=KeyPurpose(self.purpose),
            material=self.material,
            created_at=self.created_at,
            retired_at=self.retired_at,
        )


class CredentialModel(base.UUIDAuditBase):
    __tablename__ = "credential"

    name: Mapped[str] = mapped_column(unique=True, index=True)
    provider: Mapped[str] = mapped_column(index=True)
    # Data-key Fernet ciphertext of the JSON secret values.
    encrypted_values: Mapped[str] = mapped_column()
    # Which keyring data key encrypted `encrypted_values` (envelope encryption).
    key_id: Mapped[UUID] = mapped_column(ForeignKey("secret_key.id"))

    def to_entity(self) -> Credential:
        return Credential(
            id=self.id,
            name=self.name,
            provider=Provider(self.provider),
            created_at=self.created_at,
        )


class ApiKeyBudgetModel(base.UUIDAuditBase):
    """A per-key spend cap inside the team's cap (Plan 13 Phase 4).

    `api_key_id` is unique: one cap per key, replaced on write. ON DELETE
    CASCADE because a cap for a deleted key would be a row nothing can ever
    reach again, and the key's usage history is kept separately anyway.
    """

    __tablename__ = "api_key_budget"

    api_key_id: Mapped[UUID] = mapped_column(
        ForeignKey("api_key.id", ondelete="CASCADE"), unique=True, index=True
    )
    # Denormalized so the window's spend can be scoped by team without a join,
    # and so a cap can be listed per team for the console.
    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    limit_cost: Mapped[Decimal] = mapped_column(Money)
    window: Mapped[str] = mapped_column()
    mode: Mapped[str] = mapped_column()

    def to_entity(self) -> ApiKeyBudget:
        return ApiKeyBudget(
            id=self.id,
            api_key_id=self.api_key_id,
            team_id=self.team_id,
            limit_cost=self.limit_cost,
            window=BudgetWindow(self.window),
            mode=KeyBudgetMode(self.mode),
            created_at=self.created_at,
        )


class GuardrailRuleModel(base.UUIDAuditBase):
    """One configured guardrail provider in a team's chain (Plan 06).

    `model_id` NULL means the rule applies to every model the team can call; a
    row bound to a model overrides the team-wide rows for it (see
    `domain.entities.guardrail.resolve_chain`). The provider's knobs live in
    `config` as JSON, validated per kind in `domain.guardrail_config` — a column
    per knob would make adding a provider a migration.
    """

    __tablename__ = "guardrail_rule"
    __table_args__ = (
        # A name identifies the rule to an operator, and it is what appears in a
        # verdict, so it must be unambiguous within a team.
        UniqueConstraint("team_id", "name"),
        Index("ix_guardrail_rule_team_direction", "team_id", "direction"),
    )

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    # No FK cascade concerns beyond the team: a deleted model leaves its rules
    # behind harmlessly (they resolve for a model_id nothing can call anymore),
    # and ON DELETE CASCADE would silently widen a per-model exception into a
    # team-wide rule if the FK were dropped instead.
    model_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model.id", ondelete="CASCADE"), default=None, index=True
    )
    # Scope to the router the caller asked for (outranks `model_id` — see
    # `resolve_chain`). Cascades for the same reason: a deleted router must not
    # leave a rule that silently widens into a team-wide one.
    router_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("router.id", ondelete="CASCADE"), default=None, index=True
    )
    name: Mapped[str] = mapped_column()
    kind: Mapped[str] = mapped_column()
    direction: Mapped[str] = mapped_column()
    position: Mapped[int] = mapped_column(default=0)
    fail_policy: Mapped[str] = mapped_column()
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # Data-key Fernet ciphertext of the webhook signing secret (same envelope
    # scheme as CredentialModel.encrypted_values). NULL for kinds that need no
    # secret.
    encrypted_secret: Mapped[str | None] = mapped_column(default=None)
    key_id: Mapped[UUID | None] = mapped_column(ForeignKey("secret_key.id"), default=None)

    def to_entity(self) -> GuardrailRule:
        return GuardrailRule(
            id=self.id,
            team_id=self.team_id,
            model_id=self.model_id,
            router_id=self.router_id,
            name=self.name,
            kind=GuardrailKind(self.kind),
            direction=Direction(self.direction),
            position=self.position,
            fail_policy=FailPolicy(self.fail_policy),
            enabled=self.enabled,
            config=dict(self.config or {}),
            has_secret=self.encrypted_secret is not None,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class SsoSettingsModel(base.UUIDAuditBase):
    """The single OIDC identity provider configured for this deployment — at
    most one row (enforced by the repository, not the schema)."""

    __tablename__ = "sso_settings"

    enabled: Mapped[bool] = mapped_column(default=False)
    discovery_url: Mapped[str | None] = mapped_column(default=None)
    client_id: Mapped[str | None] = mapped_column(default=None)
    # Data-key Fernet ciphertext of the client secret (same envelope scheme as
    # CredentialModel.encrypted_values).
    encrypted_client_secret: Mapped[str | None] = mapped_column(default=None)
    key_id: Mapped[UUID | None] = mapped_column(ForeignKey("secret_key.id"), default=None)
    scopes: Mapped[str] = mapped_column()
    admin_groups: Mapped[list[str]] = mapped_column(JSON, default=list)
    default_admin: Mapped[bool] = mapped_column(default=False)
    team_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    redirect_uri: Mapped[str | None] = mapped_column(default=None)

    def to_entity(self) -> SsoSettings:
        return SsoSettings(
            id=self.id,
            enabled=self.enabled,
            discovery_url=self.discovery_url,
            client_id=self.client_id,
            scopes=self.scopes,
            admin_groups=tuple(self.admin_groups),
            default_admin=self.default_admin,
            team_mapping=parse_team_mapping(self.team_mapping),
            redirect_uri=self.redirect_uri,
            created_at=self.created_at,
            updated_at=self.updated_at,
            has_client_secret=self.encrypted_client_secret is not None,
        )


# Named `ModelRecord` (not `ModelModel`) to avoid the awkward double "Model".
class ModelRecord(base.UUIDAuditBase):
    __tablename__ = "model"
    __table_args__ = (
        # Unique per owning team. NULLs are distinct in a UNIQUE, so this does
        # NOT constrain global models (team_id IS NULL); the partial index below
        # keeps global names unique on their own.
        UniqueConstraint("team_id", "name"),
        Index(
            "uq_global_model_name",
            "name",
            unique=True,
            sqlite_where=text("team_id IS NULL"),
            postgresql_where=text("team_id IS NULL"),
        ),
        # A negative rate makes `domain.pricing.compute_cost` return a credit,
        # which settlement writes into the ledger the budget gate reads
        # (ISSUE-022). `ModelService` refuses one on every write path; these
        # keep a future writer that bypasses the service from reintroducing it.
        # `image_prices` is JSON and has no portable CHECK — its values are
        # covered by the application validation only.
        *(
            CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"ck_model_{column}_non_neg")
            for column in (
                "input_cost_per_token",
                "output_cost_per_token",
                "cache_write_cost_per_token",
                "cache_read_cost_per_token",
                "image_cost_per_image",
            )
        ),
    )

    # NULL ⇒ a global (platform) model, callable by every team.
    team_id: Mapped[UUID | None] = mapped_column(ForeignKey("team.id"), index=True, default=None)
    name: Mapped[str] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column()
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credential.id"))
    type: Mapped[str] = mapped_column()
    provider_model_id: Mapped[str] = mapped_column()
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    params_enforced: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    max_output_tokens: Mapped[int | None] = mapped_column(default=None)
    api_version: Mapped[str | None] = mapped_column(default=None)
    input_cost_per_token: Mapped[float | None] = mapped_column(default=None)
    output_cost_per_token: Mapped[float | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    # The originally-owning team, kept when a model is promoted to global.
    origin_team_id: Mapped[UUID | None] = mapped_column(default=None)
    # Response cache opt-in (Plan 04 Phase 0) — see domain/entities/model.py.
    cache_enabled: Mapped[bool] = mapped_column(default=False)
    cache_allow_nondeterministic: Mapped[bool] = mapped_column(default=False)
    # Semantic-tier opt-in (Plan 04 Phase 2) — see domain/entities/model.py.
    cache_semantic_enabled: Mapped[bool] = mapped_column(default=False)
    # Non-token pricing (Plan 13 Phase 1) — see domain/entities/model.py.
    cache_write_cost_per_token: Mapped[float | None] = mapped_column(default=None)
    cache_read_cost_per_token: Mapped[float | None] = mapped_column(default=None)
    image_cost_per_image: Mapped[float | None] = mapped_column(default=None)
    image_prices: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    # Declared gateway operations (Plan 18) — see domain/entities/model.py.
    # Stored as a JSON list; empty/NULL reads back as the chat-only default, so
    # every pre-existing row backfills to it without a data migration.
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)

    def to_entity(self) -> Model:
        return Model(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            provider=Provider(self.provider),
            credential_id=self.credential_id,
            type=ModelType(self.type),
            provider_model_id=self.provider_model_id,
            params=self.params or {},
            params_enforced=self.params_enforced or {},
            max_output_tokens=self.max_output_tokens,
            api_version=self.api_version,
            input_cost_per_token=self.input_cost_per_token,
            output_cost_per_token=self.output_cost_per_token,
            enabled=self.enabled,
            created_at=self.created_at,
            origin_team_id=self.origin_team_id,
            cache_enabled=self.cache_enabled,
            cache_allow_nondeterministic=self.cache_allow_nondeterministic,
            cache_semantic_enabled=self.cache_semantic_enabled,
            cache_write_cost_per_token=self.cache_write_cost_per_token,
            cache_read_cost_per_token=self.cache_read_cost_per_token,
            image_cost_per_image=self.image_cost_per_image,
            image_prices=self.image_prices or {},
            capabilities=frozenset(self.capabilities) or DEFAULT_CAPABILITIES,
        )


class ModelGrantRecord(base.UUIDAuditBase):
    """A team-owned model extended to another team, under `alias`.

    Points at the source model (no copy); the target team calls it by `alias`.
    Global models need no grant rows — they resolve to every team by name.
    """

    __tablename__ = "model_grant"
    __table_args__ = (
        UniqueConstraint("team_id", "alias"),
        # A model is extended to a given team at most once.
        UniqueConstraint("model_id", "team_id"),
    )

    model_id: Mapped[UUID] = mapped_column(ForeignKey("model.id", ondelete="CASCADE"), index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    alias: Mapped[str] = mapped_column()

    def to_entity(self) -> ModelGrant:
        return ModelGrant(
            id=self.id,
            model_id=self.model_id,
            team_id=self.team_id,
            alias=self.alias,
            created_at=self.created_at,
        )


class CallableAliasRecord(base.UUIDAuditBase):
    """One explicit callable binding shared by models and routers.

    Effective ``-global`` aliases are derived by the resolver from one snapshot;
    only user/admin-declared names and grant aliases are persisted here.
    """

    __tablename__ = "callable_alias"
    __table_args__ = (
        UniqueConstraint("team_id", "alias"),
        Index(
            "uq_global_callable_alias",
            "alias",
            unique=True,
            sqlite_where=text("team_id IS NULL"),
            postgresql_where=text("team_id IS NULL"),
        ),
        Index(
            "uq_callable_alias_direct_model",
            "model_id",
            unique=True,
            sqlite_where=text("model_id IS NOT NULL AND model_grant_id IS NULL"),
            postgresql_where=text("model_id IS NOT NULL AND model_grant_id IS NULL"),
        ),
        Index(
            "uq_callable_alias_direct_router",
            "router_id",
            unique=True,
            sqlite_where=text("router_id IS NOT NULL AND router_grant_id IS NULL"),
            postgresql_where=text("router_id IS NOT NULL AND router_grant_id IS NULL"),
        ),
        CheckConstraint(
            "(unavailable AND model_id IS NULL AND router_id IS NULL "
            "AND model_grant_id IS NULL AND router_grant_id IS NULL) OR "
            "(NOT unavailable AND ((model_id IS NOT NULL AND router_id IS NULL) OR "
            "(model_id IS NULL AND router_id IS NOT NULL)))",
            name="ck_callable_alias_state",
        ),
        CheckConstraint(
            "model_grant_id IS NULL OR (model_id IS NOT NULL AND router_grant_id IS NULL)",
            name="ck_callable_alias_model_grant_target",
        ),
        CheckConstraint(
            "router_grant_id IS NULL OR (router_id IS NOT NULL AND model_grant_id IS NULL)",
            name="ck_callable_alias_router_grant_target",
        ),
        CheckConstraint(
            "(model_grant_id IS NULL AND router_grant_id IS NULL) OR team_id IS NOT NULL",
            name="ck_callable_alias_grant_team_scope",
        ),
    )

    team_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("team.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str] = mapped_column(index=True)
    unavailable: Mapped[bool] = mapped_column(default=False)
    model_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model.id", ondelete="CASCADE"), index=True, default=None
    )
    router_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("router.id", ondelete="CASCADE"), index=True, default=None
    )
    model_grant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_grant.id", ondelete="CASCADE"), unique=True, default=None
    )
    router_grant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("router_grant.id", ondelete="CASCADE"), unique=True, default=None
    )


class ServicePrincipalModel(base.UUIDAuditBase):
    """A team-owned machine identity; its keys carry management scope."""

    __tablename__ = "service_principal"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    name: Mapped[str] = mapped_column()
    enabled: Mapped[bool] = mapped_column(default=True)

    def to_entity(self) -> ServicePrincipal:
        return ServicePrincipal(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            enabled=self.enabled,
            created_at=self.created_at,
        )


class APIKeyModel(base.UUIDAuditBase):
    """`UUIDAuditBase` provides `id`, `created_at`, `updated_at`."""

    __tablename__ = "api_key"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_account.id"), index=True)
    name: Mapped[str | None] = mapped_column(default=None)
    prefix: Mapped[str] = mapped_column(index=True)
    key_hash: Mapped[str] = mapped_column(unique=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    scope: Mapped[str] = mapped_column(default=KeyScope.INFERENCE.value)
    service_principal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("service_principal.id"), default=None, index=True
    )
    rate_limit_rpm: Mapped[int | None] = mapped_column(default=None)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> APIKey:
        return APIKey(
            id=self.id,
            team_id=self.team_id,
            created_by=self.created_by,
            name=self.name,
            prefix=self.prefix,
            key_hash=self.key_hash,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
            last_used_at=self.last_used_at,
            expires_at=self.expires_at,
            scope=KeyScope(self.scope),
            service_principal_id=self.service_principal_id,
            rate_limit_rpm=self.rate_limit_rpm,
        )


class McpServerModel(base.UUIDAuditBase):
    """A registered MCP tool server (Plan 20).

    `team_id` NULL means global — the same spelling a global `Model` uses, which
    is what lets one visibility union serve both instead of a per-resource rule.

    The bearer token is envelope-encrypted exactly like `guardrail_rule`'s signing
    secret: ciphertext plus the data key that sealed it, decrypted only on the
    call path. No `to_entity` ever carries it, so no response can leak it.
    """

    __tablename__ = "mcp_server"
    __table_args__ = (
        # A name identifies the server to an operator and appears in a tool
        # error, so it must be unambiguous within its owner. Two teams may both
        # have a "github"; a global one is unique on its own (team_id NULL).
        UniqueConstraint("team_id", "name"),
    )

    # Nullable for a global server. No `ondelete` on purpose: a team's servers
    # are removed explicitly by the purge child list, which is where ISSUE-040
    # was found for `guardrail_rule` — the same table is registered there from
    # this slice rather than after a review notices it.
    team_id: Mapped[UUID | None] = mapped_column(ForeignKey("team.id"), default=None, index=True)
    name: Mapped[str] = mapped_column()
    url: Mapped[str] = mapped_column()
    enabled: Mapped[bool] = mapped_column(default=True)
    tool_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    encrypted_auth: Mapped[str | None] = mapped_column(default=None)
    auth_key_id: Mapped[UUID | None] = mapped_column(ForeignKey("secret_key.id"), default=None)
    # When discovery last succeeded. NULL means it never ran, which is a
    # different fact from "it ran and the server offers nothing" — and without
    # this column the two are the same empty tool list, so the console cannot
    # tell a server nobody has queried from one that genuinely has no tools.
    # It is also the "last discovery" the console shows as a health signal.
    last_discovered_at: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> McpServer:
        return McpServer(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            url=self.url,
            enabled=self.enabled,
            created_at=self.created_at,
            has_auth=self.encrypted_auth is not None,
            tool_allowlist=tuple(self.tool_allowlist or ()),
            last_discovered_at=self.last_discovered_at,
            # `global` is the one origin readable from the row alone — a NULL
            # `team_id` is global to every viewer. `own` vs `extended` depends on
            # who is asking, so `visible_to` decides those. Defaulting this to
            # `own` made the platform surface report a global server as owned.
            origin=(CallableOrigin.GLOBAL if self.team_id is None else CallableOrigin.OWN),
        )


class McpServerGrantModel(base.UUIDAuditBase):
    """A team-owned server extended to another team, mirroring `model_grant`.

    Global servers need no grant rows — they resolve to every team.
    """

    __tablename__ = "mcp_server_grant"
    __table_args__ = (UniqueConstraint("server_id", "team_id"),)

    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_server.id", ondelete="CASCADE"), index=True
    )
    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)

    def to_entity(self) -> McpServerGrant:
        return McpServerGrant(
            id=self.id,
            server_id=self.server_id,
            team_id=self.team_id,
            created_at=self.created_at,
        )


class McpServerSuppressionModel(base.UUIDAuditBase):
    """One team's detach of a server it does not own (design §2.2).

    A separate table rather than a flag on the grant, because a *global* server
    has no grant row to carry one. Removing a global or extended server is a
    suppression here; only the platform's own delete removes the resource, which
    is what stops a team admin revoking a capability from every other tenant —
    the ISSUE-020 mistake.
    """

    __tablename__ = "mcp_server_suppression"
    __table_args__ = (UniqueConstraint("server_id", "team_id"),)

    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_server.id", ondelete="CASCADE"), index=True
    )
    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)


class McpToolModel(base.UUIDAuditBase):
    """A tool discovered from a server, with the effect an operator declared.

    The inventory is a cache of `tools/list`; `effect` is not — it is operator
    state that survives re-discovery, because a value re-read from the server on
    every refresh would be a value the server controls.
    """

    __tablename__ = "mcp_tool"
    __table_args__ = (UniqueConstraint("server_id", "name"),)

    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("mcp_server.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column()
    description: Mapped[str] = mapped_column(default="")
    schema: Mapped[dict] = mapped_column(JSON, default=dict)
    # Defaults to the most dangerous class: a tool nobody classified must not be
    # invocable under the permissive per-key default.
    effect: Mapped[str] = mapped_column(default=ToolEffect.DESTRUCTIVE.value)
    discovered_at: Mapped[datetime | None] = mapped_column(default=None)

    def to_entity(self) -> McpTool:
        return McpTool(
            id=self.id,
            server_id=self.server_id,
            name=self.name,
            description=self.description,
            schema=dict(self.schema or {}),
            effect=ToolEffect(self.effect),
            discovered_at=self.discovered_at,
        )


class ApiKeyToolPolicyModel(base.UUIDAuditBase):
    """Per-key tool restriction, on the `api_key_budget` precedent.

    `api_key_id` is unique: one policy per key, replaced on write, CASCADE on the
    key for the same reason a cap cascades — a policy for a deleted key is a row
    nothing can reach. Absent means unrestricted; the row exists mainly to carry
    `destructive_enabled`, which is the one thing the permissive default excludes.
    """

    __tablename__ = "api_key_tool_policy"

    api_key_id: Mapped[UUID] = mapped_column(
        ForeignKey("api_key.id", ondelete="CASCADE"), unique=True, index=True
    )
    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    destructive_enabled: Mapped[bool] = mapped_column(default=False)

    def to_entity(self) -> ApiKeyToolPolicy:
        return ApiKeyToolPolicy(
            id=self.id,
            api_key_id=self.api_key_id,
            team_id=self.team_id,
            allowed_tools=tuple(self.allowed_tools or ()),
            destructive_enabled=self.destructive_enabled,
            created_at=self.created_at,
        )
