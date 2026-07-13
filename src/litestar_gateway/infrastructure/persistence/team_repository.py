"""SQLAlchemy adapter implementing the `TeamRepository` port."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Team
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import (
    PendingUsageEventModel,
    RouterModel,
    ServicePrincipalModel,
    TeamBudgetModel,
    TeamMembershipModel,
    TeamModel,
    UsageEventModel,
)


class SQLAlchemyTeamRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, team: Team) -> Team:
        # Stage only (flush, no commit); the service owns the transaction boundary.
        model = TeamModel(
            id=team.id,
            organization_id=team.organization_id,
            name=team.name,
            description=team.description,
            tags=list(team.tags),
            rate_limit_rpm=team.rate_limit_rpm,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, team_id: UUID) -> Team | None:
        model = await self._session.get(TeamModel, team_id)
        return model.to_entity() if model else None

    async def list_by_organization(
        self, organization_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel)
            .where(TeamModel.organization_id == organization_id)
            .order_by(TeamModel.created_at)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def list(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel).order_by(TeamModel.created_at).limit(limit).offset(offset)
        )
        return [m.to_entity() for m in models]

    async def update(
        self,
        team_id: UUID,
        name: str,
        description: str | None,
        tags: Sequence[str],
        rate_limit_rpm: int | None,
    ) -> Team | None:
        # Stage only (flush); the service owns the commit (unit of work).
        model = await self._session.get(TeamModel, team_id)
        if model is None:
            return None
        model.name = name
        model.description = description
        model.tags = list(tags)
        model.rate_limit_rpm = rate_limit_rpm
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def delete(self, team_id: UUID) -> None:
        # Remove the intrinsic children first (all FK team.id with RESTRICT, so
        # the team row can't drop while they exist), then the team. Models and
        # API keys are intentionally NOT touched — the caller refuses the delete
        # when any remain. pending_usage_event has no FK but is team-scoped, so
        # it's cleared for hygiene. Staged only; the service commits.
        for child in (
            UsageEventModel,
            PendingUsageEventModel,
            RouterModel,
            ServicePrincipalModel,
            TeamMembershipModel,
            TeamBudgetModel,
        ):
            await self._session.execute(delete(child).where(child.team_id == team_id))
        await self._session.execute(delete(TeamModel).where(TeamModel.id == team_id))
        await self._session.flush()
