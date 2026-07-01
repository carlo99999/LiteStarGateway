"""SQLAlchemy adapter implementing the `InviteRepository` port."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import Invite
from litestar_test.infrastructure.persistence.orm import InviteModel


class SQLAlchemyInviteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, invite: Invite) -> Invite:
        model = InviteModel(
            id=invite.id,
            token_hash=invite.token_hash,
            expires_at=invite.expires_at,
            used_at=invite.used_at,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        model = await self._session.scalar(
            select(InviteModel).where(InviteModel.token_hash == token_hash)
        )
        return model.to_entity() if model else None

    async def mark_used(self, invite_id: UUID, used_at: datetime) -> bool:
        """Atomically consume the invite. Returns False if it was already used
        (conditional UPDATE), so concurrent signups can't reuse one invite."""
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            update(InviteModel)
            .where(InviteModel.id == invite_id, InviteModel.used_at.is_(None))
            .values(used_at=used_at)
        )
        await self._session.commit()
        return result.rowcount == 1
