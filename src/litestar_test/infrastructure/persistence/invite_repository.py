"""SQLAlchemy adapter implementing the `InviteRepository` port."""

from __future__ import annotations

from sqlalchemy import select
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

    async def update(self, invite: Invite) -> Invite:
        model = await self._session.get(InviteModel, invite.id)
        if model is None:  # pragma: no cover - guarded by callers
            raise LookupError(f"Invite {invite.id} disappeared")
        model.used_at = invite.used_at
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()
