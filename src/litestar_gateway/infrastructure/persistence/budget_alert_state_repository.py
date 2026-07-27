"""SQLAlchemy adapter implementing the `BudgetAlertStateRepository` port."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import BudgetAlertState, BudgetWindow, PendingBudgetAlert
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import (
    BudgetAlertStateModel,
    PendingBudgetAlertModel,
)


class SQLAlchemyBudgetAlertStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fired_thresholds(
        self, team_id: UUID, window: BudgetWindow, period_start: datetime
    ) -> set[int]:
        rows = await self._session.scalars(
            select(BudgetAlertStateModel.threshold).where(
                BudgetAlertStateModel.team_id == team_id,
                BudgetAlertStateModel.window == window.value,
                BudgetAlertStateModel.period_start == period_start,
            )
        )
        return set(rows.all())

    async def record_fired(
        self,
        team_id: UUID,
        window: BudgetWindow,
        period_start: datetime,
        threshold: int,
    ) -> BudgetAlertState | None:
        row = BudgetAlertStateModel(
            id=uuid4(),
            team_id=team_id,
            window=window.value,
            period_start=period_start,
            threshold=threshold,
            fired_at=datetime.now(UTC),
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            # Concurrent settlement (this replica or another) already recorded
            # this exact dedup key — the unique constraint made the loser's
            # insert a conflict rather than a duplicate row. Not an error: the
            # threshold is fired either way.
            await self._session.rollback()
            return None
        await self._session.refresh(row)
        return row.to_entity()

    async def enqueue_alert(self, alert: PendingBudgetAlert) -> None:
        self._session.add(
            PendingBudgetAlertModel(
                id=alert.id,
                team_id=alert.team_id,
                window=alert.window.value,
                period_start=alert.period_start,
                threshold=alert.threshold,
                spend=alert.spend,
                limit_cost=alert.limit_cost,
            )
        )
        await self._session.commit()

    async def pending_alerts(self, *, limit: int = DEFAULT_PAGE_SIZE) -> list[PendingBudgetAlert]:
        rows = await self._session.scalars(
            select(PendingBudgetAlertModel)
            .order_by(PendingBudgetAlertModel.created_at)
            .limit(limit)
        )
        return [row.to_entity() for row in rows.all()]
