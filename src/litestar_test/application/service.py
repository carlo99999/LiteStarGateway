"""Application service — orchestrates the API key use cases.

Depends only on the `APIKeyRepository` port and pure domain logic, never on
SQLAlchemy or Litestar. This is the hexagon's core.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_test.domain.entities import APIKey, IssuedKey
from litestar_test.domain.exceptions import APIKeyNotFound, InvalidAPIKey
from litestar_test.domain.key_generator import generate_key, hash_key
from litestar_test.domain.ports import APIKeyRepository


def _now() -> datetime:
    return datetime.now(UTC)


class APIKeyService:
    def __init__(self, repository: APIKeyRepository) -> None:
        self._repo = repository

    async def issue(self, team_id: UUID, created_by: UUID, name: str | None = None) -> IssuedKey:
        material = generate_key()
        key = APIKey(
            id=uuid4(),
            team_id=team_id,
            created_by=created_by,
            name=name,
            prefix=material.prefix,
            key_hash=material.key_hash,
            created_at=_now(),
            revoked_at=None,
            last_used_at=None,
        )
        stored = await self._repo.add(key)
        return IssuedKey(key=stored, plaintext=material.plaintext)

    async def authenticate(self, plaintext: str) -> APIKey:
        key = await self._repo.get_by_hash(hash_key(plaintext))
        if key is None or not key.is_active:
            raise InvalidAPIKey("Invalid or revoked API key")
        return await self._repo.update(dataclasses.replace(key, last_used_at=_now()))

    async def revoke_for_team(self, team_id: UUID, key_id: UUID) -> None:
        key = await self._repo.get(key_id)
        if key is None or key.team_id != team_id:
            raise APIKeyNotFound(str(key_id))
        if key.is_active:
            await self._repo.update(dataclasses.replace(key, revoked_at=_now()))

    async def list_for_team(self, team_id: UUID) -> list[APIKey]:
        return await self._repo.list_by_team(team_id)
