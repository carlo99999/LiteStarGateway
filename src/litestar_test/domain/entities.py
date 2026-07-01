"""Domain entities — pure, framework- and persistence-agnostic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class TeamRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class KeyPurpose(StrEnum):
    """What a keyring key is used for."""

    CREDENTIAL = "credential"  # encrypts credential values at rest
    JWT = "jwt"  # signs login JWTs


@dataclass(frozen=True)
class UsageEvent:
    """One recorded model call: token counts and estimated cost, tagged with the
    API key and model so usage can be broken down by either."""

    id: UUID
    team_id: UUID
    api_key_id: UUID
    model_id: UUID
    model_name: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    created_at: datetime


@dataclass(frozen=True)
class UsageAggregate:
    """Usage summed for one model (over an optional api-key/model filter)."""

    model_id: UUID
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    calls: int


@dataclass(frozen=True)
class ExternalIdentity:
    """An identity resolved from an SSO provider's id_token claims."""

    subject: str  # the IdP's stable user id (`sub`)
    email: str
    email_verified: bool  # the IdP asserts it verified this address
    groups: tuple[str, ...]


@dataclass(frozen=True)
class TraceRecord:
    """One observability trace for a model call (metadata; no payload in v1)."""

    team_id: UUID
    api_key_id: UUID
    model_name: str
    provider: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    latency_ms: float
    status: str
    created_at: datetime


@dataclass(frozen=True)
class SecretKey:
    """A rotating keyring key. `material` is the master-wrapped key bytes; only
    the wrapped form is persisted. Retired keys are kept for decrypt/verify only.
    """

    id: UUID
    purpose: KeyPurpose
    material: str
    created_at: datetime
    retired_at: datetime | None

    @property
    def is_usable(self) -> bool:
        return self.retired_at is None


class Provider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    VERTEX_AI = "vertex_ai"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    DATABRICKS = "databricks"


class ModelType(StrEnum):
    CHAT = "chat"
    IMAGE = "image"
    EMBEDDINGS = "embeddings"


@dataclass(frozen=True)
class Credential:
    """Metadata for a provider credential. Secret values are stored encrypted
    by the repository and never live on this entity."""

    id: UUID
    name: str
    provider: Provider
    created_at: datetime


@dataclass(frozen=True)
class Model:
    """A configured model deployment owned by a team.

    `provider` must match the referenced credential's provider (enforced on write).
    """

    id: UUID
    team_id: UUID
    name: str
    provider: Provider
    credential_id: UUID
    type: ModelType
    provider_model_id: str  # upstream model name, e.g. "gpt-4o"
    params: dict[str, Any]  # default LLM params (temperature, max_tokens, ...)
    # Note: no api_base here — the endpoint comes from the (admin-managed)
    # credential, so a team admin cannot redirect the credential's secret.
    api_version: str | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class Organization:
    """A container of teams. Has no direct users."""

    id: UUID
    name: str
    created_at: datetime


@dataclass(frozen=True)
class Team:
    """A team belongs to exactly one organization."""

    id: UUID
    organization_id: UUID
    name: str
    created_at: datetime


@dataclass(frozen=True)
class TeamMembership:
    """A user's membership in a team, with a role."""

    id: UUID
    team_id: UUID
    user_id: UUID
    role: TeamRole
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role is TeamRole.ADMIN


@dataclass(frozen=True)
class APIKey:
    """An issued API key, always owned by a team. Only the hash is persisted."""

    id: UUID
    team_id: UUID
    created_by: UUID
    name: str | None
    prefix: str
    key_hash: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class IssuedKey:
    """Result of issuing a key: the entity plus the one-time plaintext."""

    key: APIKey
    plaintext: str


@dataclass(frozen=True)
class User:
    """A user account. Only the password hash is ever persisted."""

    id: UUID
    email: str
    password_hash: str
    is_admin: bool
    created_at: datetime
    # Bumped on logout to invalidate previously issued JWTs.
    token_version: int = 0
    # The IdP subject (`sub`) this account is federated to, once linked via SSO.
    # None for password-only accounts. Stable across the user's email changes.
    sso_subject: str | None = None


@dataclass(frozen=True)
class Invite:
    """A single-use invitation. Only the token hash is persisted."""

    id: UUID
    token_hash: str
    created_at: datetime
    used_at: datetime | None

    @property
    def is_usable(self) -> bool:
        return self.used_at is None


@dataclass(frozen=True)
class IssuedInvite:
    """Result of creating an invite: the entity plus the one-time token."""

    invite: Invite
    token: str


@dataclass(frozen=True)
class PasswordReset:
    """A single-use, expiring admin-issued password reset for a specific user.

    Only the token hash is persisted; the plaintext is shown once to the admin,
    who relays it. The user redeems it and chooses their own new password.
    """

    id: UUID
    user_id: UUID
    token_hash: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None

    def is_usable(self, now: datetime) -> bool:
        return self.used_at is None and now < self.expires_at


@dataclass(frozen=True)
class IssuedPasswordReset:
    """Result of creating a reset: the entity plus the one-time token."""

    reset: PasswordReset
    token: str
