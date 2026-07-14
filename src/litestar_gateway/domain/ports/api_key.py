"""Port — API key persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import APIKey
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class APIKeyRepository(Protocol):
    """Persistence port for API keys."""

    async def add(self, key: APIKey) -> APIKey: ...

    async def get(self, key_id: UUID) -> APIKey | None: ...

    async def get_by_hash(self, key_hash: str) -> APIKey | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]: ...

    async def list_by_creator(
        self, created_by: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]:
        """Keys the user created (their `created_by`) — used to guard user
        deletion so a key's creator is never orphaned."""
        ...

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
