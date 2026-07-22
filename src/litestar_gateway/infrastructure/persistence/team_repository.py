"""SQLAlchemy adapter implementing the `TeamRepository` port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import Team
from litestar_gateway.domain.exceptions import TeamNotEmpty
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.callable_alias_slots import (
    lock_resource_lifecycle,
    tombstone_resource,
)
from litestar_gateway.infrastructure.persistence.orm import (
    InviteModel,
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

    async def list_by_ids(self, team_ids: Sequence[UUID]) -> list[Team]:
        if not team_ids:
            return []
        models = await self._session.scalars(select(TeamModel).where(TeamModel.id.in_(team_ids)))
        return [model.to_entity() for model in models]

    async def lock_for_lifecycle(self, team_id: UUID) -> Team | None:
        # A no-op write is a cross-database lifecycle mutex: PostgreSQL takes a
        # row-level NO KEY UPDATE lock and SQLite takes its writer lock. Raw SQL
        # avoids SQLAlchemy's automatic `updated_at` value on ORM updates.
        statement = text("UPDATE team SET name = name WHERE id = :team_id").bindparams(
            bindparam("team_id", type_=TeamModel.id.type)
        )
        result: Any = await self._session.execute(statement, {"team_id": team_id})
        if result.rowcount != 1:
            return None
        model = await self._session.get(TeamModel, team_id)
        return model.to_entity() if model else None

    async def list_by_organization(
        self, organization_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel)
            .where(TeamModel.organization_id == organization_id)
            .order_by(TeamModel.created_at, TeamModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def list(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel)
            .order_by(TeamModel.created_at, TeamModel.id)
            .limit(limit)
            .offset(offset)
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
        try:
            router_ids = list(
                await self._session.scalars(
                    select(RouterModel.id)
                    .where(RouterModel.team_id == team_id)
                    .order_by(RouterModel.id)
                )
            )
            for router_id in router_ids:
                router = await lock_resource_lifecycle(
                    self._session, CallableKind.ROUTER, router_id
                )
                if not isinstance(router, RouterModel) or router.team_id != team_id:
                    continue
                await tombstone_resource(self._session, CallableKind.ROUTER, router_id)
            for child in (
                InviteModel,
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
        except IntegrityError as exc:
            # A concurrent non-intrinsic child (model/API key) may appear after
            # the service recheck. Preserve the lifecycle contract as a 409,
            # never leak a database 500; the service UoW rolls everything back.
            raise TeamNotEmpty(str(team_id)) from exc
