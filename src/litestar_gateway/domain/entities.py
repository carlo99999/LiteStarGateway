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


class KeyScope(StrEnum):
    """What an API key may do. The key is a team-owned service principal;
    its scope bounds it to inference, team management, or both."""

    INFERENCE = "inference"  # the /v1/* endpoints only (default)
    MANAGEMENT = "management"  # team-scoped management, own team only
    ALL = "all"

    @property
    def allows_inference(self) -> bool:
        return self in (KeyScope.INFERENCE, KeyScope.ALL)

    @property
    def allows_management(self) -> bool:
        return self in (KeyScope.MANAGEMENT, KeyScope.ALL)


class BudgetWindow(StrEnum):
    """Spend window a budget applies to. Calendar-based, UTC."""

    MONTHLY = "monthly"
    DAILY = "daily"


@dataclass(frozen=True)
class Budget:
    """A hard spend cap (USD) for a team over a recurring calendar window.

    Enforcement is pre-call: once the window's accumulated cost reaches
    `limit_cost`, further inference calls are rejected. Requests already in
    flight when the limit is crossed may still complete (bounded overshoot)."""

    id: UUID
    team_id: UUID
    limit_cost: float
    window: BudgetWindow
    created_at: datetime


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
class AuditEvent:
    """An append-only record of a privileged action: who did what, to what, from
    where, and when. `actor_email` is denormalized so the log stays readable even
    if the user is later removed. Never store secrets in `detail`."""

    id: UUID
    action: str  # "<resource>.<verb>", e.g. "credential.create", "api_key.revoke"
    actor_id: UUID | None
    # Disambiguates actor_id ("user" | "api_key"): user ids and key ids share
    # the column, and a reader must never join a key id against users.
    actor_type: str | None
    actor_email: str | None
    target_type: str | None
    target_id: str | None
    ip: str | None
    detail: str | None
    created_at: datetime


@dataclass(frozen=True)
class ApiKeySpend:
    """Accumulated usage/cost for one API key across all of its calls."""

    api_key_id: UUID
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
    # Exception class name when status == "error" (no message: keep traces secret-free).
    error_type: str | None = None


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
class ServicePrincipal:
    """A team-owned, named machine identity (Databricks-style). Its keys carry
    management scope; disabling it stops all of them. Administered via a human
    JWT — a key can never create or manage service principals."""

    id: UUID
    team_id: UUID
    name: str
    enabled: bool
    created_at: datetime


@dataclass(frozen=True)
class APIKey:
    """An issued API key. Only the hash is persisted.

    A key is either **personal** (`service_principal_id is None`) — owned by and
    attributed to its `created_by` user, inference-only, revoked when that user
    is deactivated — or a **service-principal key** (`service_principal_id` set),
    which is the only kind that may hold management/all scope."""

    id: UUID
    team_id: UUID
    created_by: UUID
    name: str | None
    prefix: str
    key_hash: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    scope: KeyScope = KeyScope.INFERENCE
    service_principal_id: UUID | None = None

    @property
    def is_service_principal(self) -> bool:
        return self.service_principal_id is not None

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
    # A disabled account cannot authenticate (login JWTs are rejected). Toggled by
    # a platform admin; disabling also revokes existing sessions (token_version bump).
    is_active: bool = True
    # Per-account brute-force defense: consecutive failed password logins, and a
    # temporary lock once the threshold is hit. Reset on successful login.
    failed_login_attempts: int = 0
    locked_until: datetime | None = None


@dataclass(frozen=True)
class Principal:
    """The acting identity behind a request: a human user (JWT) or a team
    service principal (API key with management scope). Exactly one is set.

    A key principal is bounded to its own team and is never a platform admin —
    platform surfaces (credentials, organizations, audit, invites, key
    minting) require a human JWT."""

    user: User | None = None
    api_key: APIKey | None = None
    # Loaded when api_key belongs to a service principal — the acting identity
    # for authorization (must be enabled) and audit attribution.
    service_principal: ServicePrincipal | None = None

    @property
    def is_platform_admin(self) -> bool:
        return self.user is not None and self.user.is_admin

    @property
    def audit_label(self) -> str:
        """Attribution for the audit trail: user email, the service-principal
        name, or (personal key) the key's public prefix."""
        if self.user is not None:
            return self.user.email
        if self.service_principal is not None:
            return f"sp:{self.service_principal.name}"
        return f"api-key:{self.api_key.prefix}" if self.api_key else "unknown"

    @property
    def audit_actor_id(self) -> UUID | None:
        if self.user is not None:
            return self.user.id
        # Attribute to the identity, not the credential: the SP if there is one.
        if self.service_principal is not None:
            return self.service_principal.id
        return self.api_key.id if self.api_key else None

    @property
    def audit_actor_type(self) -> str:
        if self.user is not None:
            return "user"
        return "service_principal" if self.service_principal is not None else "api_key"


@dataclass(frozen=True)
class Invite:
    """A single-use, expiring invitation. Only the token hash is persisted."""

    id: UUID
    token_hash: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None

    def is_usable(self, now: datetime) -> bool:
        return self.used_at is None and now < self.expires_at


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
