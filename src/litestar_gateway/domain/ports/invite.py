"""Port — invites and password resets."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Invite, PasswordReset


class InviteRepository(Protocol):
    """Persistence port for invites."""

    async def add(self, invite: Invite) -> Invite:
        """Stage an invite insert; the surrounding unit of work owns the commit.

        Raise TeamNotFound if the invite's team disappeared before the insert.
        """
        ...

    async def get_by_token_hash(self, token_hash: str) -> Invite | None: ...

    async def mark_used(self, invite_id: UUID, used_at: datetime) -> bool:
        """Stage single-use consumption; the surrounding unit of work commits."""
        ...


class PasswordResetRepository(Protocol):
    """Persistence port for admin-issued password resets."""

    async def add(self, reset: PasswordReset) -> PasswordReset: ...

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None: ...

    async def mark_used(self, reset_id: UUID, used_at: datetime) -> bool: ...
