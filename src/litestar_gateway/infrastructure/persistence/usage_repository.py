"""SQLAlchemy adapter implementing the `UsageRepository` port."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import ApiKeySpend, UsageAggregate, UsageEvent
from litestar_test.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_test.infrastructure.persistence.orm import PendingUsageEventModel, UsageEventModel

logger = logging.getLogger("litestar_test.usage")


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
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[UsageAggregate]:
        # One row per model (GROUP BY), but a team's model count is unbounded, so
        # this pages like every other list query.
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
            .limit(limit)
            .offset(offset)
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

    async def spend_by_api_key(self, team_id: UUID) -> list[ApiKeySpend]:
        query = (
            select(
                UsageEventModel.api_key_id,
                func.coalesce(func.sum(UsageEventModel.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.completion_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.cost), 0.0),
                func.count(),
            )
            .where(UsageEventModel.team_id == team_id)
            .group_by(UsageEventModel.api_key_id)
        )
        rows = (await self._session.execute(query)).all()
        return [
            ApiKeySpend(
                api_key_id=row[0],
                prompt_tokens=int(row[1]),
                completion_tokens=int(row[2]),
                cost=float(row[3]),
                calls=int(row[4]),
            )
            for row in rows
        ]

    async def enqueue_pending(self, event: UsageEvent) -> None:
        # The request session may be in a failed state after the ledger commit
        # failed; roll back before writing the dead-letter row.
        await self._session.rollback()
        self._session.add(
            PendingUsageEventModel(
                event_id=event.id,
                team_id=event.team_id,
                api_key_id=event.api_key_id,
                model_id=event.model_id,
                model_name=event.model_name,
                operation=event.operation,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cost=event.cost,
                event_created_at=event.created_at,
            )
        )
        await self._session.commit()

    async def reconcile_pending(self, *, limit: int = DEFAULT_PAGE_SIZE) -> int:
        pending = (
            await self._session.scalars(
                select(PendingUsageEventModel)
                .order_by(PendingUsageEventModel.created_at)
                .limit(limit)
            )
        ).all()
        settled = 0
        for row in pending:
            try:
                # Idempotent: skip the ledger insert if the event already landed.
                if await self._session.get(UsageEventModel, row.event_id) is None:
                    self._session.add(
                        UsageEventModel(
                            id=row.event_id,
                            team_id=row.team_id,
                            api_key_id=row.api_key_id,
                            model_id=row.model_id,
                            model_name=row.model_name,
                            operation=row.operation,
                            prompt_tokens=row.prompt_tokens,
                            completion_tokens=row.completion_tokens,
                            cost=row.cost,
                        )
                    )
                await self._session.delete(row)
                await self._session.commit()
                settled += 1
            except Exception:  # leave it queued for the next cycle
                await self._session.rollback()
                logger.warning("failed to reconcile pending usage event", exc_info=True)
        return settled
