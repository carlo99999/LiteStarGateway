"""Port — SCIM provisioning-token persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import ScimToken


class ScimTokenRepository(Protocol):
    """Persistence port for SCIM provisioning tokens."""

    async def add(self, token: ScimToken) -> ScimToken: ...

    async def get_by_token_hash(self, token_hash: str) -> ScimToken | None: ...

    async def list(self) -> list[ScimToken]: ...

    async def revoke(self, token_id: UUID, revoked_at: datetime) -> bool:
        """Mark the token revoked; False when it doesn't exist. Idempotent — an
        already-revoked token keeps its original revocation time."""
        ...
