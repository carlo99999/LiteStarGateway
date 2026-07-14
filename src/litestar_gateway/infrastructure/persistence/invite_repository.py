"""SQLAlchemy adapter implementing the `InviteRepository` port."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Invite
from litestar_gateway.domain.exceptions import TeamNotFound
from litestar_gateway.infrastructure.persistence.orm import InviteModel


class SQLAlchemyInviteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, invite: Invite) -> Invite:
        model = InviteModel(
            id=invite.id,
            token_hash=invite.token_hash,
            expires_at=invite.expires_at,
            used_at=invite.used_at,
            team_id=invite.team_id,
            role=invite.role,
        )
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            constraint_name = getattr(exc.orig, "constraint_name", None)
            message = str(exc.orig).lower()
            team_fk_names = ("fk_invite_team_id_team", "invite_team_id_fkey")
            missing_team = constraint_name in team_fk_names or any(
                name in message for name in team_fk_names
            )
            missing_team = missing_team or "foreign key constraint failed" in message
            if invite.team_id is not None and missing_team:
                raise TeamNotFound(str(invite.team_id)) from exc
            raise
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
            .where(
                InviteModel.id == invite_id,
                InviteModel.used_at.is_(None),
                InviteModel.expires_at > used_at,
            )
            .values(used_at=used_at)
        )
        return result.rowcount == 1
