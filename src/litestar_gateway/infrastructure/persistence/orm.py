"""SQLAlchemy ORM mappings (persistence detail)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from advanced_alchemy.extensions.litestar import base
from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from litestar_gateway.domain.entities import (
    APIKey,
    AuditEvent,
    Budget,
    BudgetWindow,
    Credential,
    Invite,
    KeyPurpose,
    KeyScope,
    Model,
    ModelGrant,
    ModelType,
    Organization,
    PasswordReset,
    Provider,
    ScimToken,
    SecretKey,
    ServicePrincipal,
    Team,
    TeamMembership,
    TeamRole,
    UsageEvent,
    User,
)
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RouterGrant,
    RoutingDecisionRecord,
)


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

    def to_entity(self) -> Team:
        return Team(
            id=self.id,
            organization_id=self.organization_id,
            name=self.name,
            created_at=self.created_at,
            description=self.description,
            tags=list(self.tags or []),
            rate_limit_rpm=self.rate_limit_rpm,
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
    operation: Mapped[str] = mapped_column()
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float] = mapped_column(default=0.0)

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
    operation: Mapped[str] = mapped_column()
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    event_created_at: Mapped[datetime] = mapped_column()
    # Poison-message bookkeeping: rows that keep failing the ledger insert are
    # quarantined after MAX_RECONCILE_ATTEMPTS instead of starving the batch.
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(default=None)


class TeamBudgetModel(base.UUIDAuditBase):
    """A team's hard spend cap (at most one row per team)."""

    __tablename__ = "team_budget"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), unique=True, index=True)
    limit_cost: Mapped[float] = mapped_column()
    window: Mapped[str] = mapped_column()

    def to_entity(self) -> Budget:
        return Budget(
            id=self.id,
            team_id=self.team_id,
            limit_cost=self.limit_cost,
            window=BudgetWindow(self.window),
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
