"""Identity, authentication, and user invitation entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities.access import APIKey, ServicePrincipal


@dataclass(frozen=True)
class ExternalIdentity:
    """An identity resolved from an SSO provider's id_token claims."""

    subject: str  # the IdP's stable user id (`sub`)
    email: str
    email_verified: bool  # the IdP asserts it verified this address
    groups: tuple[str, ...]


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
    # Which lever disabled the account: "admin" or "scim" (None while active).
    # SCIM may only reactivate accounts it deactivated itself — an IdP full-sync
    # must not resurrect an account an admin disabled for cause.
    deactivated_by: str | None = None
    # The IdP's SCIM `externalId` for this account, once provisioned/adopted via
    # SCIM. None for accounts the IdP does not manage.
    external_id: str | None = None
    # Platform auditor: read-only access to the audit log and to every team's
    # usage/budget figures. Granted/revoked by a platform admin; never implies
    # any write access. Orthogonal to is_admin (an admin already sees it all).
    is_auditor: bool = False
    # Per-account brute-force defense: consecutive failed password logins, and a
    # temporary lock once the threshold is hit. Reset on successful login.
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    # Consecutive lock cycles: each one doubles the next lock's duration
    # (capped), so re-locking the account the moment a lock expires gets
    # progressively costlier. Reset on successful login or after a quiet decay.
    lockout_cycles: int = 0


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


@dataclass(frozen=True)
class ScimToken:
    """An admin-issued SCIM provisioning token — the IdP's credential for the
    /scim/v2 surface. Only the token hash is persisted. It does not expire (the
    IdP holds it long-term); rotation is an explicit admin revoke + re-mint."""

    id: UUID
    name: str
    token_hash: str
    created_at: datetime
    revoked_at: datetime | None

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class IssuedScimToken:
    """Result of minting a SCIM token: the entity plus the one-time plaintext."""

    scim_token: ScimToken
    token: str
