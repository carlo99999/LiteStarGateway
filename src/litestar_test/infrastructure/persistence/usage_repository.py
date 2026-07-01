"""SQLAlchemy adapter implementing the `UsageRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import UsageAggregate, UsageEvent
from litestar_test.infrastructure.persistence.orm import UsageEventModel


class SQLAlchemyUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, event: UsageEvent) -> None:
        self._session.add(
            UsageEventModel(
                id=event.id,
                team_id=event.team_id,
                api_key_id=event.api_key_id,
                model_id=event.model_id,
                model_name=event.model_name,
                operation=event.operation,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cost=event.cost,
            )
        )
        await self._session.commit()

    async def aggregate(
        self,
        team_id: UUID,
        *,
        model_name: str | None = None,
        api_key_id: UUID | None = None,
    ) -> list[UsageAggregate]:
        query = (
            select(
                UsageEventModel.model_id,
                UsageEventModel.model_name,
                func.coalesce(func.sum(UsageEventModel.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.completion_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.cost), 0.0),
                func.count(),
            )
            .where(UsageEventModel.team_id == team_id)
            .group_by(UsageEventModel.model_id, UsageEventModel.model_name)
            .order_by(UsageEventModel.model_name)
        )
        if model_name is not None:
            query = query.where(UsageEventModel.model_name == model_name)
        if api_key_id is not None:
            query = query.where(UsageEventModel.api_key_id == api_key_id)

        rows = (await self._session.execute(query)).all()
        return [
            UsageAggregate(
                model_id=row[0],
                model_name=row[1],
                prompt_tokens=int(row[2]),
                completion_tokens=int(row[3]),
                cost=float(row[4]),
                calls=int(row[5]),
            )
            for row in rows
        ]
