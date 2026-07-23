"""Identity, authentication, and user invitation entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities.access import APIKey, ServicePrincipal
from litestar_gateway.domain.entities.enums import TeamRole


@dataclass(frozen=True)
class ExternalIdentity:
    """An identity resolved from an SSO provider's id_token claims."""

    subject: str  # the IdP's stable user id (`sub`)
    email: str
    email_verified: bool  # the IdP asserts it verified this address
    groups: tuple[str, ...]


@dataclass(frozen=True)
class TeamGrant:
    """One (team, role) an IdP group confers via an SSO team mapping."""

    team_id: UUID
    role: TeamRole


def parse_team_mapping(data: Mapping[str, object]) -> dict[str, tuple[TeamGrant, ...]]:
    """Validate a decoded JSON object mapping each IdP group to a list of
    ``{"team": "<team-uuid>", "role": "admin"|"member"}`` grants (role defaults
    to member). Raises `ValueError` on malformed input — callers translate it
    into whichever error fits their layer (startup config vs an admin API
    request). Layer-agnostic on purpose: shared by env-var parsing
    (`config.py`) and the DB-backed SSO settings service."""
    mapping: dict[str, tuple[TeamGrant, ...]] = {}
    for group, grants in data.items():
        if not isinstance(grants, list):
            raise ValueError(f"{group!r} must map to a list of grants")
        parsed: list[TeamGrant] = []
        for grant in grants:
            if not isinstance(grant, dict) or "team" not in grant:
                raise ValueError(f"{group!r} entries need a 'team' UUID")
            try:
                team_id = UUID(str(grant["team"]))
                role = TeamRole(grant.get("role", TeamRole.MEMBER))
            except ValueError as exc:
                raise ValueError(f"{group!r} has an invalid team or role: {exc}") from exc
            parsed.append(TeamGrant(team_id=team_id, role=role))
        mapping[group] = tuple(parsed)
    # Two different non-admin roles for one team would make the resolved role
    # depend on the IdP's group ordering (ADMIN always wins, so pairing it with
    # another role stays deterministic). Reject the ambiguity.
    non_admin_roles: dict[UUID, TeamRole] = {}
    for grants_ in mapping.values():
        for grant_ in grants_:
            if grant_.role is TeamRole.ADMIN:
                continue
            seen = non_admin_roles.setdefault(grant_.team_id, grant_.role)
            if seen is not grant_.role:
                raise ValueError(
                    f"team {grant_.team_id} is mapped to conflicting roles "
                    f"'{seen}' and '{grant_.role}'; grant one non-admin role per team"
                )
    return mapping


def team_mapping_to_json(
    mapping: dict[str, tuple[TeamGrant, ...]],
) -> dict[str, list[dict[str, str]]]:
    """Inverse of `parse_team_mapping` — a JSON-serializable form for storage
    (DB column or env var)."""
    return {
        group: [{"team": str(grant.team_id), "role": grant.role.value} for grant in grants]
        for group, grants in mapping.items()
    }


@dataclass(frozen=True)
class SsoSettings:
    """The single OIDC identity provider configured for this deployment
    (self-hosted, one IdP per instance). Persisted so a platform admin can
    manage it from the console instead of environment variables + a restart.

    Never carries `client_secret` in the clear — the repository stores it
    encrypted (envelope encryption, same scheme as provider credentials) and
    only decrypts it internally to build the OAuth2 client."""

    id: UUID
    enabled: bool
    discovery_url: str | None
    client_id: str | None
    scopes: str
    admin_groups: tuple[str, ...]
    default_admin: bool
    team_mapping: dict[str, tuple[TeamGrant, ...]]
    redirect_uri: str | None
    created_at: datetime
    updated_at: datetime
    has_client_secret: bool = False


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
    # The team (and role) the invitee joins on signup. Required for new invites;
    # nullable for backward compatibility with any pre-existing teamless invite.
    team_id: UUID | None = None
    role: str | None = None

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
