"""Port — API key persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import APIKey
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class APIKeyRepository(Protocol):
    """Persistence port for API keys."""

    async def add(self, key: APIKey) -> APIKey:
        """Stage a key insert; the application service owns the commit."""
        ...

    async def get(self, key_id: UUID) -> APIKey | None: ...

    async def get_for_update(self, key_id: UUID) -> APIKey | None:
        """Load and lock a key for a transactional state change."""
        ...

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

    async def touch_last_used(self, key_id: UUID, last_used_at: datetime) -> bool:
        """Update only ``last_used_at`` while the key is still active."""
        ...

    async def is_authenticatable(self, key_id: UUID) -> bool:
        """Validate key and owning principal state in one current DB statement."""
        ...

    async def schedule_revocation(
        self,
        key_id: UUID,
        expected_revoked_at: datetime | None,
        revoked_at: datetime,
    ) -> bool:
        """Stage a compare-and-set of ``revoked_at`` for atomic rotation."""
        ...

    async def revoke_personal_keys_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        """Revoke all active *personal* keys (no service principal) created by
        the user — called when the account is deactivated. Staged only."""
        ...

    async def revoke_for_service_principal(
        self, service_principal_id: UUID, revoked_at: datetime
    ) -> None:
        """Stage immediate revocation and detach keys from a deleted principal."""
        ...
