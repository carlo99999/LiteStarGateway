"""SQLAlchemy adapter implementing the `BudgetAlertStateRepository` port."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import BudgetAlertState, BudgetWindow
from litestar_gateway.infrastructure.persistence.orm import BudgetAlertStateModel


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
