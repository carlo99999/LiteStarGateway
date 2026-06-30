"""SQLAlchemy adapter implementing the `APIKeyRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import APIKey
from litestar_test.infrastructure.persistence.orm import APIKeyModel


class SQLAlchemyAPIKeyRepository:
    """Maps between `APIKey` domain entities and `APIKeyModel` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, key: APIKey) -> APIKey:
        model = APIKeyModel(
            id=key.id,
            team_id=key.team_id,
            created_by=key.created_by,
            name=key.name,
            prefix=key.prefix,
            key_hash=key.key_hash,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, key_id: UUID) -> APIKey | None:
        model = await self._session.get(APIKeyModel, key_id)
        return model.to_entity() if model else None

    async def get_by_hash(self, key_hash: str) -> APIKey | None:
        model = await self._session.scalar(
            select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
        )
        return model.to_entity() if model else None

    async def list_by_team(self, team_id: UUID) -> list[APIKey]:
        models = await self._session.scalars(
            select(APIKeyModel)
            .where(APIKeyModel.team_id == team_id)
            .order_by(APIKeyModel.created_at)
        )
        return [m.to_entity() for m in models]

    async def update(self, key: APIKey) -> APIKey:
        model = await self._session.get(APIKeyModel, key.id)
        if model is None:  # pragma: no cover - guarded by callers
            raise LookupError(f"APIKey {key.id} disappeared")
        model.revoked_at = key.revoked_at
        model.last_used_at = key.last_used_at
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()
