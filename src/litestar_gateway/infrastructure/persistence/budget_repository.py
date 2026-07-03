"""SQLAlchemy adapter implementing the `BudgetRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Budget
from litestar_gateway.infrastructure.persistence.orm import TeamBudgetModel


class SQLAlchemyBudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, team_id: UUID) -> Budget | None:
        row = await self._session.scalar(
            select(TeamBudgetModel).where(TeamBudgetModel.team_id == team_id)
        )
        return row.to_entity() if row else None

    async def set(self, budget: Budget) -> Budget:
        try:
            return await self._upsert(budget)
        except IntegrityError:
            # Concurrent insert for the same team lost the unique-constraint
            # race; retry once — the row now exists, so this becomes an update.
            await self._session.rollback()
            return await self._upsert(budget)

    async def _upsert(self, budget: Budget) -> Budget:
        row = await self._session.scalar(
            select(TeamBudgetModel).where(TeamBudgetModel.team_id == budget.team_id)
        )
        if row is None:
            row = TeamBudgetModel(
                id=budget.id,
                team_id=budget.team_id,
                limit_cost=budget.limit_cost,
                window=budget.window.value,
            )
            self._session.add(row)
        else:
            row.limit_cost = budget.limit_cost
            row.window = budget.window.value
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def remove(self, team_id: UUID) -> None:
        await self._session.execute(
            delete(TeamBudgetModel).where(TeamBudgetModel.team_id == team_id)
        )
        await self._session.commit()
