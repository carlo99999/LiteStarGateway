"""SQLAlchemy adapter implementing the `SecretKeyRepository` port."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import KeyPurpose, SecretKey
from litestar_test.infrastructure.persistence.orm import SecretKeyModel


class SQLAlchemySecretKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, key: SecretKey) -> SecretKey:
        model = SecretKeyModel(
            id=key.id,
            purpose=key.purpose.value,
            material=key.material,
            retired_at=key.retired_at,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, key_id: UUID) -> SecretKey | None:
        model = await self._session.get(SecretKeyModel, key_id)
        return model.to_entity() if model else None

    async def get_active(self, purpose: KeyPurpose) -> SecretKey | None:
        """The newest usable (non-retired) key for the purpose."""
        model = await self._session.scalar(
            select(SecretKeyModel)
            .where(SecretKeyModel.purpose == purpose.value, SecretKeyModel.retired_at.is_(None))
            .order_by(SecretKeyModel.created_at.desc())
            .limit(1)
        )
        return model.to_entity() if model else None

    async def list_usable(self, purpose: KeyPurpose) -> list[SecretKey]:
        models = await self._session.scalars(
            select(SecretKeyModel)
            .where(SecretKeyModel.purpose == purpose.value, SecretKeyModel.retired_at.is_(None))
            .order_by(SecretKeyModel.created_at.desc())
        )
        return [m.to_entity() for m in models]

    async def retire(self, key_id: UUID, retired_at: datetime) -> None:
        await self._session.execute(
            update(SecretKeyModel)
            .where(SecretKeyModel.id == key_id, SecretKeyModel.retired_at.is_(None))
            .values(retired_at=retired_at)
        )
        await self._session.commit()

    async def delete(self, key_id: UUID) -> None:
        await self._session.execute(delete(SecretKeyModel).where(SecretKeyModel.id == key_id))
        await self._session.commit()
