"""SQLAlchemy adapter implementing the `TeamMembershipRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import TeamMembership
from litestar_test.infrastructure.persistence.orm import TeamMembershipModel


class SQLAlchemyTeamMembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, membership: TeamMembership) -> TeamMembership:
        # Stage only (flush, no commit); the service owns the transaction boundary.
        model = TeamMembershipModel(
            id=membership.id,
            team_id=membership.team_id,
            user_id=membership.user_id,
            role=membership.role.value,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, team_id: UUID, user_id: UUID) -> TeamMembership | None:
        model = await self._session.scalar(
            select(TeamMembershipModel).where(
                TeamMembershipModel.team_id == team_id,
                TeamMembershipModel.user_id == user_id,
            )
        )
        return model.to_entity() if model else None

    async def list_by_team(self, team_id: UUID) -> list[TeamMembership]:
        models = await self._session.scalars(
            select(TeamMembershipModel)
            .where(TeamMembershipModel.team_id == team_id)
            .order_by(TeamMembershipModel.created_at)
        )
        return [m.to_entity() for m in models]

    async def update(self, membership: TeamMembership) -> TeamMembership:
        model = await self._session.get(TeamMembershipModel, membership.id)
        if model is None:  # pragma: no cover - guarded by callers
            raise LookupError(f"Membership {membership.id} disappeared")
        model.role = membership.role.value
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def remove(self, team_id: UUID, user_id: UUID) -> None:
        await self._session.execute(
            delete(TeamMembershipModel).where(
                TeamMembershipModel.team_id == team_id,
                TeamMembershipModel.user_id == user_id,
            )
        )
