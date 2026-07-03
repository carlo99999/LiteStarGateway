"""Ports — interfaces the application depends on. Adapters implement these."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import (
    APIKey,
    ApiKeySpend,
    AuditEvent,
    Budget,
    Credential,
    ExternalIdentity,
    Invite,
    KeyPurpose,
    Model,
    Organization,
    PasswordReset,
    SecretKey,
    ServicePrincipal,
    Team,
    TeamMembership,
    TraceRecord,
    UsageAggregate,
    UsageEvent,
    User,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class APIKeyRepository(Protocol):
    """Persistence port for API keys."""

    async def add(self, key: APIKey) -> APIKey: ...

    async def get(self, key_id: UUID) -> APIKey | None: ...

    async def get_by_hash(self, key_hash: str) -> APIKey | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]: ...

    async def update(self, key: APIKey) -> APIKey: ...

    async def revoke_personal_keys_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        """Revoke all active *personal* keys (no service principal) created by
        the user — called when the account is deactivated."""
        ...

    async def revoke_for_service_principal(
        self, service_principal_id: UUID, revoked_at: datetime
    ) -> None:
        """Revoke all of a service principal's active keys (on SP deletion)."""
        ...


class CredentialRepository(Protocol):
    """Persistence port for provider credentials.

    The adapter encrypts `values` at rest; metadata reads never expose secrets.
    """

    async def add(self, credential: Credential, values: dict[str, str]) -> Credential: ...

    async def get(self, credential_id: UUID) -> Credential | None: ...

    async def get_by_name(self, name: str) -> Credential | None: ...

    async def list(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Credential]: ...

    async def get_values(self, credential_id: UUID) -> dict[str, str] | None: ...

    async def remove(self, credential_id: UUID) -> None: ...


@runtime_checkable
class LLMGateway(Protocol):
    """Port for calling LLM providers in an OpenAI-compatible way.

    Takes an OpenAI-shaped `request`, the resolved `model` (provider, upstream id,
    params, endpoint overrides) and the decrypted credential `values`. Returns an
    OpenAI-shaped response dict. Sync and async variants mirror the OpenAI SDK;
    the sync ones block and must never be called from an async handler.
    """

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve eagerly (await) and return an async iterator of OpenAI
        `chat.completion.chunk` dicts. Resolution errors surface before streaming."""
        ...

    async def astream_responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve eagerly and return an async iterator of Responses-API stream
        event dicts (each carries a `type`)."""
        ...

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...


class ModelRepository(Protocol):
    """Persistence port for team-scoped model deployments."""

    async def add(self, model: Model) -> Model: ...

    async def get(self, model_id: UUID) -> Model | None: ...

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]: ...

    async def update(self, model: Model) -> Model: ...

    async def remove(self, model_id: UUID) -> None: ...


class OrganizationRepository(Protocol):
    """Persistence port for organizations."""

    async def add(self, organization: Organization) -> Organization: ...

    async def get(self, organization_id: UUID) -> Organization | None: ...

    async def list(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Organization]: ...


class TeamRepository(Protocol):
    """Persistence port for teams."""

    async def add(self, team: Team) -> Team: ...

    async def get(self, team_id: UUID) -> Team | None: ...

    async def list_by_organization(self, organization_id: UUID) -> list[Team]: ...


class TeamMembershipRepository(Protocol):
    """Persistence port for team memberships."""

    async def add(self, membership: TeamMembership) -> TeamMembership: ...

    async def get(self, team_id: UUID, user_id: UUID) -> TeamMembership | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[TeamMembership]: ...

    async def update(self, membership: TeamMembership) -> TeamMembership: ...

    async def remove(self, team_id: UUID, user_id: UUID) -> None: ...


class UserRepository(Protocol):
    """Persistence port for users."""

    async def add(self, user: User) -> User: ...

    async def get(self, user_id: UUID) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_sso_subject(self, subject: str) -> User | None: ...

    async def count(self) -> int: ...

    async def set_active(self, user_id: UUID, is_active: bool) -> None:
        """Enable/disable the account; disabling also bumps token_version to revoke
        the user's existing sessions."""
        ...

    async def bind_sso(self, user_id: UUID, sso_subject: str, is_admin: bool) -> User:
        """Link an account to an IdP subject and set its admin flag (IdP is the
        source of truth for SSO role), returning the updated user."""
        ...

    async def increment_token_version(self, user_id: UUID) -> None: ...

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        """Set a new password hash and bump token_version (revoking old JWTs)."""
        ...

    async def register_failed_login(self, user_id: UUID) -> int:
        """Atomically increment the failed-login counter; returns the new count."""
        ...

    async def set_login_lock(self, user_id: UUID, locked_until: datetime) -> None:
        """Temporarily lock password logins and reset the failure counter."""
        ...

    async def clear_login_failures(self, user_id: UUID) -> None:
        """Reset the failure counter and any lock (after a successful login)."""
        ...


class InviteRepository(Protocol):
    """Persistence port for invites."""

    async def add(self, invite: Invite) -> Invite: ...

    async def get_by_token_hash(self, token_hash: str) -> Invite | None: ...

    async def mark_used(self, invite_id: UUID, used_at: datetime) -> bool: ...


class PasswordResetRepository(Protocol):
    """Persistence port for admin-issued password resets."""

    async def add(self, reset: PasswordReset) -> PasswordReset: ...

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None: ...

    async def mark_used(self, reset_id: UUID, used_at: datetime) -> bool: ...


@runtime_checkable
class UsageRepository(Protocol):
    """Persistence port for recorded usage events + aggregation."""

    async def record(self, event: UsageEvent) -> None: ...

    async def aggregate(
        self,
        team_id: UUID,
        *,
        model_name: str | None = None,
        api_key_id: UUID | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[UsageAggregate]:
        """Usage summed per model for a team, optionally filtered by model name
        and/or API key. One row per model, paged like every other list query."""
        ...

    async def spend_by_api_key(self, team_id: UUID) -> list[ApiKeySpend]:
        """Token/cost totals grouped by API key for the team (includes keys that
        are now revoked, as long as they have recorded usage)."""
        ...

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        """Total cost recorded for the team from `since` onwards. Read on the
        hot path by the budget gate — must stay a cheap indexed aggregate."""
        ...

    async def enqueue_pending(self, event: UsageEvent) -> None:
        """Durable dead-letter for a usage event whose ledger write failed, so a
        background reconciler can retry it instead of the event being lost."""
        ...

    async def reconcile_pending(self, *, limit: int = DEFAULT_PAGE_SIZE) -> int:
        """Move up to `limit` dead-lettered usage events into the ledger (idempotent
        by event id), removing settled ones. Returns how many were settled."""
        ...


class ServicePrincipalRepository(Protocol):
    """Persistence port for team service principals."""

    async def add(self, sp: ServicePrincipal) -> ServicePrincipal: ...

    async def get(self, sp_id: UUID) -> ServicePrincipal | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[ServicePrincipal]: ...

    async def set_enabled(self, sp_id: UUID, enabled: bool) -> ServicePrincipal: ...

    async def remove(self, sp_id: UUID) -> None: ...


@runtime_checkable
class BudgetRepository(Protocol):
    """Persistence port for per-team spend caps (at most one budget per team)."""

    async def get(self, team_id: UUID) -> Budget | None: ...

    async def set(self, budget: Budget) -> Budget:
        """Create the team's budget, or replace it if one exists (upsert)."""
        ...

    async def remove(self, team_id: UUID) -> None: ...


@runtime_checkable
class AuditLog(Protocol):
    """Append-only audit trail of privileged actions."""

    async def record(self, event: AuditEvent) -> None: ...

    async def list_recent(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[AuditEvent]:
        """Most recent audit events first (for the admin read API)."""
        ...


@runtime_checkable
class IdentityProvider(Protocol):
    """SSO provider: build the login redirect and resolve the callback code.

    `nonce` binds the id_token to this authorization request (replay defense);
    `code_verifier` is the PKCE secret whose S256 challenge rides the redirect
    (authorization-code interception defense). Both are generated per login and
    verified on exchange."""

    async def authorization_url(
        self, state: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> str: ...

    async def exchange(
        self, code: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> ExternalIdentity: ...


class DistributedLock(Protocol):
    """Cross-process mutual exclusion, e.g. so only one replica runs key rotation."""

    def hold(self, name: str, *, ttl: timedelta) -> AbstractAsyncContextManager[bool]:
        """Async context manager that yields True if the lock was acquired (held
        for the block, auto-expiring after `ttl`), or False if another holder has it."""
        ...


class TraceSink(Protocol):
    """Observability sink. `write` is synchronous — the dispatcher's worker calls
    it off the event loop (MLflow's client is blocking)."""

    def write(self, record: TraceRecord) -> None: ...


class SecretKeyRepository(Protocol):
    """Persistence port for the rotating keyring (envelope encryption)."""

    async def add(self, key: SecretKey) -> SecretKey: ...

    async def get(self, key_id: UUID) -> SecretKey | None: ...

    async def get_active(self, purpose: KeyPurpose) -> SecretKey | None: ...

    async def list_usable(self, purpose: KeyPurpose) -> list[SecretKey]: ...

    async def retire(self, key_id: UUID, retired_at: datetime) -> None: ...

    async def delete(self, key_id: UUID) -> None: ...


class Transaction(Protocol):
    """Unit-of-work boundary for a single use-case.

    Repositories participating in a transactional flow only *stage* writes
    (flush, no commit); the service commits once via this port, so a multi-step
    operation either persists fully or not at all. `AsyncSession` satisfies it.

    Project convention: a repository method that is the *only* write of its
    use case may self-commit (most single-write CRUD does); any use case with
    two or more writes MUST stage them and commit once through this port
    (see `TeamService` and `UserService.reset_password`). Deliberate
    exceptions are documented at the call site (e.g. `UserService.register`
    burns the invite in its own transaction as an anti-enumeration cost, and
    the usage ledger writes autonomously so billing is fail-safe).
    """

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
