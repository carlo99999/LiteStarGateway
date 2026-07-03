"""SQLAlchemy adapter implementing the `TeamRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Team
from litestar_gateway.infrastructure.persistence.orm import TeamModel


class SQLAlchemyTeamRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, team: Team) -> Team:
        # Stage only (flush, no commit); the service owns the transaction boundary.
        model = TeamModel(id=team.id, organization_id=team.organization_id, name=team.name)
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, team_id: UUID) -> Team | None:
        model = await self._session.get(TeamModel, team_id)
        return model.to_entity() if model else None

    async def list_by_organization(self, organization_id: UUID) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel)
            .where(TeamModel.organization_id == organization_id)
            .order_by(TeamModel.created_at)
        )
        return [m.to_entity() for m in models]
