"""SQLAlchemy adapter implementing the `PasswordResetRepository` port."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import PasswordReset
from litestar_gateway.infrastructure.persistence.orm import PasswordResetModel


class SQLAlchemyPasswordResetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, reset: PasswordReset) -> PasswordReset:
        model = PasswordResetModel(
            id=reset.id,
            user_id=reset.user_id,
            token_hash=reset.token_hash,
            expires_at=reset.expires_at,
            used_at=reset.used_at,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None:
        model = await self._session.scalar(
            select(PasswordResetModel).where(PasswordResetModel.token_hash == token_hash)
        )
        return model.to_entity() if model else None

    async def mark_used(self, reset_id: UUID, used_at: datetime) -> bool:
        """Atomically consume the reset. Returns False if it was already used
        (conditional UPDATE), so one token can't set the password twice.

        Stages only (no commit): redeeming a reset is a unit of work with the
        password write, committed once by the service — a failure after the
        token is consumed must roll the consumption back too."""
        # Any: async execute() is typed Result, but at runtime is a CursorResult.
        result: Any = await self._session.execute(
            update(PasswordResetModel)
            .where(PasswordResetModel.id == reset_id, PasswordResetModel.used_at.is_(None))
            .values(used_at=used_at)
        )
        return result.rowcount == 1
