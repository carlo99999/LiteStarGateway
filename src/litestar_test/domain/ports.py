"""Ports — interfaces the application depends on. Adapters implement these."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from litestar_test.domain.entities import (
    APIKey,
    Credential,
    Invite,
    Model,
    Organization,
    Team,
    TeamMembership,
    User,
)


class APIKeyRepository(Protocol):
    """Persistence port for API keys."""

    async def add(self, key: APIKey) -> APIKey: ...

    async def get(self, key_id: UUID) -> APIKey | None: ...

    async def get_by_hash(self, key_hash: str) -> APIKey | None: ...

    async def list_by_team(self, team_id: UUID) -> list[APIKey]: ...

    async def update(self, key: APIKey) -> APIKey: ...


class CredentialRepository(Protocol):
    """Persistence port for provider credentials.

    The adapter encrypts `values` at rest; metadata reads never expose secrets.
    """

    async def add(self, credential: Credential, values: dict[str, str]) -> Credential: ...

    async def get(self, credential_id: UUID) -> Credential | None: ...

    async def get_by_name(self, name: str) -> Credential | None: ...

    async def list(self) -> list[Credential]: ...

    async def get_values(self, credential_id: UUID) -> dict[str, str] | None: ...

    async def remove(self, credential_id: UUID) -> None: ...


# runtime_checkable: this port is injected as a dependency, and Litestar/msgspec
# runs isinstance() on the resolved value during signature validation.
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

    async def list_by_team(self, team_id: UUID) -> list[Model]: ...

    async def update(self, model: Model) -> Model: ...

    async def remove(self, model_id: UUID) -> None: ...


class OrganizationRepository(Protocol):
    """Persistence port for organizations."""

    async def add(self, organization: Organization) -> Organization: ...

    async def get(self, organization_id: UUID) -> Organization | None: ...

    async def list(self) -> list[Organization]: ...


class TeamRepository(Protocol):
    """Persistence port for teams."""

    async def add(self, team: Team) -> Team: ...

    async def get(self, team_id: UUID) -> Team | None: ...

    async def list_by_organization(self, organization_id: UUID) -> list[Team]: ...


class TeamMembershipRepository(Protocol):
    """Persistence port for team memberships."""

    async def add(self, membership: TeamMembership) -> TeamMembership: ...

    async def get(self, team_id: UUID, user_id: UUID) -> TeamMembership | None: ...

    async def list_by_team(self, team_id: UUID) -> list[TeamMembership]: ...

    async def update(self, membership: TeamMembership) -> TeamMembership: ...

    async def remove(self, team_id: UUID, user_id: UUID) -> None: ...


class UserRepository(Protocol):
    """Persistence port for users."""

    async def add(self, user: User) -> User: ...

    async def get(self, user_id: UUID) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def count(self) -> int: ...

    async def increment_token_version(self, user_id: UUID) -> None: ...


class InviteRepository(Protocol):
    """Persistence port for invites."""

    async def add(self, invite: Invite) -> Invite: ...

    async def get_by_token_hash(self, token_hash: str) -> Invite | None: ...

    async def mark_used(self, invite_id: UUID, used_at: datetime) -> bool: ...


class Transaction(Protocol):
    """Unit-of-work boundary for a single use-case.

    Repositories participating in a transactional flow only *stage* writes
    (flush, no commit); the service commits once via this port, so a multi-step
    operation either persists fully or not at all. `AsyncSession` satisfies it.
    """

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
