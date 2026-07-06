"""Port — rotating keyring persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import KeyPurpose, SecretKey


class SecretKeyRepository(Protocol):
    """Persistence port for the rotating keyring (envelope encryption)."""

    async def add(self, key: SecretKey) -> SecretKey: ...

    async def get(self, key_id: UUID) -> SecretKey | None: ...

    async def get_active(self, purpose: KeyPurpose) -> SecretKey | None: ...

    async def list_usable(self, purpose: KeyPurpose) -> list[SecretKey]: ...

    async def retire(self, key_id: UUID, retired_at: datetime) -> None: ...

    async def delete(self, key_id: UUID) -> None: ...
