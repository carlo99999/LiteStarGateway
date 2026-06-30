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
