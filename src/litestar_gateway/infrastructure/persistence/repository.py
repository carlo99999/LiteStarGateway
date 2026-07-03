"""SQLAlchemy adapter implementing the `APIKeyRepository` port."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import APIKey
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import APIKeyModel


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
            scope=key.scope.value,
            service_principal_id=key.service_principal_id,
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

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]:
        models = await self._session.scalars(
            select(APIKeyModel)
            .where(APIKeyModel.team_id == team_id)
            .order_by(APIKeyModel.created_at)
            .limit(limit)
            .offset(offset)
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

    async def revoke_personal_keys_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        await self._session.execute(
            update(APIKeyModel)
            .where(
                APIKeyModel.created_by == user_id,
                APIKeyModel.service_principal_id.is_(None),
                APIKeyModel.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        await self._session.commit()

    async def revoke_for_service_principal(
        self, service_principal_id: UUID, revoked_at: datetime
    ) -> None:
        await self._session.execute(
            update(APIKeyModel)
            .where(
                APIKeyModel.service_principal_id == service_principal_id,
                APIKeyModel.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        await self._session.commit()
