"""SQLAlchemy ORM mappings (persistence detail)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from advanced_alchemy.extensions.litestar import base
from sqlalchemy import JSON, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from litestar_gateway.domain.entities import (
    APIKey,
    AuditEvent,
    Budget,
    BudgetWindow,
    Credential,
    Invite,
    KeyPurpose,
    Model,
    ModelType,
    Organization,
    PasswordReset,
    Provider,
    SecretKey,
    Team,
    TeamMembership,
    TeamRole,
    UsageEvent,
    User,
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
    failed_login_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)

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
            failed_login_attempts=self.failed_login_attempts,
            locked_until=self.locked_until,
        )


class AuditEventModel(base.UUIDAuditBase):
    __tablename__ = "audit_event"

    action: Mapped[str] = mapped_column(index=True)
    actor_id: Mapped[UUID | None] = mapped_column(default=None, index=True)
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

    def to_entity(self) -> Invite:
        return Invite(
            id=self.id,
            token_hash=self.token_hash,
            created_at=self.created_at,
            expires_at=self.expires_at,
            used_at=self.used_at,
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


class OrganizationModel(base.UUIDAuditBase):
    __tablename__ = "organization"

    name: Mapped[str] = mapped_column(index=True)

    def to_entity(self) -> Organization:
        return Organization(id=self.id, name=self.name, created_at=self.created_at)


class TeamModel(base.UUIDAuditBase):
    __tablename__ = "team"

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(index=True)

    def to_entity(self) -> Team:
        return Team(
            id=self.id,
            organization_id=self.organization_id,
            name=self.name,
            created_at=self.created_at,
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
    api_key_id: Mapped[UUID] = mapped_column(ForeignKey("api_key.id"), index=True)
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
    transient failure never silently loses a billing record."""

    __tablename__ = "pending_usage_event"

    event_id: Mapped[UUID] = mapped_column(unique=True, index=True)  # intended usage_event.id
    team_id: Mapped[UUID] = mapped_column(index=True)
    api_key_id: Mapped[UUID] = mapped_column()
    model_id: Mapped[UUID] = mapped_column()
    model_name: Mapped[str] = mapped_column()
    operation: Mapped[str] = mapped_column()
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float] = mapped_column(default=0.0)
    event_created_at: Mapped[datetime] = mapped_column()


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
    __table_args__ = (UniqueConstraint("team_id", "name"),)

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    name: Mapped[str] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column()
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credential.id"))
    type: Mapped[str] = mapped_column()
    provider_model_id: Mapped[str] = mapped_column()
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    api_version: Mapped[str | None] = mapped_column(default=None)
    input_cost_per_token: Mapped[float | None] = mapped_column(default=None)
    output_cost_per_token: Mapped[float | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(default=True)

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
            api_version=self.api_version,
            input_cost_per_token=self.input_cost_per_token,
            output_cost_per_token=self.output_cost_per_token,
            enabled=self.enabled,
            created_at=self.created_at,
        )


class APIKeyModel(base.UUIDAuditBase):
    """`UUIDAuditBase` provides `id`, `created_at`, `updated_at`."""

    __tablename__ = "api_key"

    team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_account.id"))
    name: Mapped[str | None] = mapped_column(default=None)
    prefix: Mapped[str] = mapped_column(index=True)
    key_hash: Mapped[str] = mapped_column(unique=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

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
        )
