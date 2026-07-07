"""SQLAlchemy adapter implementing the `ScimTokenRepository` port."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import ScimToken
from litestar_gateway.infrastructure.persistence.orm import ScimTokenModel


class SQLAlchemyScimTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: ScimToken) -> ScimToken:
        model = ScimTokenModel(
            id=token.id,
            name=token.name,
            token_hash=token.token_hash,
            revoked_at=token.revoked_at,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get_by_token_hash(self, token_hash: str) -> ScimToken | None:
        model = await self._session.scalar(
            select(ScimTokenModel).where(ScimTokenModel.token_hash == token_hash)
        )
        return model.to_entity() if model else None

    async def list(self) -> list[ScimToken]:
        result = await self._session.scalars(
            select(ScimTokenModel).order_by(ScimTokenModel.created_at.desc())
        )
        return [model.to_entity() for model in result]

    async def revoke(self, token_id: UUID, revoked_at: datetime) -> bool:
        # Guarded on revoked_at IS NULL so a second revoke keeps the original
        # timestamp; existence is checked separately for the idempotent True.
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            update(ScimTokenModel)
            .where(ScimTokenModel.id == token_id, ScimTokenModel.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )
        await self._session.commit()
        if result.rowcount:
            return True
        return await self._session.get(ScimTokenModel, token_id) is not None
