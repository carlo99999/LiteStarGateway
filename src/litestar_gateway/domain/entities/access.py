"""API key, secret key, and service principal entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from .enums import KeyPurpose, KeyScope


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
    # Requests/minute cap for this key; None = unlimited.
    rate_limit_rpm: int | None = None

    @property
    def is_service_principal(self) -> bool:
        return self.service_principal_id is not None

    @property
    def is_active(self) -> bool:
        """Usable right now: never revoked, or revocation scheduled in the future
        (the rotation grace window, during which the old key keeps working)."""
        if self.revoked_at is None:
            return True
        revoked = self.revoked_at if self.revoked_at.tzinfo else self.revoked_at.replace(tzinfo=UTC)
        return revoked > datetime.now(UTC)


@dataclass(frozen=True)
class IssuedKey:
    """Result of issuing a key: the entity plus the one-time plaintext."""

    key: APIKey
    plaintext: str
