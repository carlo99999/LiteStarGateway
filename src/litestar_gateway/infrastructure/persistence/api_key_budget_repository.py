"""SQLAlchemy adapter for per-key spend caps."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import ApiKeyBudget
from litestar_gateway.infrastructure.persistence.orm import ApiKeyBudgetModel


class SQLAlchemyApiKeyBudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, api_key_id: UUID) -> ApiKeyBudget | None:
        row = await self._session.scalar(
            select(ApiKeyBudgetModel).where(ApiKeyBudgetModel.api_key_id == api_key_id)
        )
        return row.to_entity() if row else None

    async def list_for_team(self, team_id: UUID) -> list[ApiKeyBudget]:
        rows = await self._session.scalars(
            select(ApiKeyBudgetModel).where(ApiKeyBudgetModel.team_id == team_id)
        )
        return [row.to_entity() for row in rows]

    async def set(self, budget: ApiKeyBudget) -> ApiKeyBudget:
        try:
            row = await self._upsert(budget)
        except IntegrityError:
            # Concurrent insert for the same key lost the unique-constraint
            # race; retry once — the row now exists, so this becomes an update.
            await self._session.rollback()
            row = await self._upsert(budget)
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def stage_set(self, budget: ApiKeyBudget) -> ApiKeyBudget:
        """`set` without the commit, for a caller that owns the transaction.

        Key rotation copies a cap onto the replacement key inside the same unit
        of work that issues it: committing here would break that atomicity and
        leave a window where the new key is live and uncapped, which is the whole
        thing the copy exists to prevent (ISSUE-052).
        """
        return (await self._upsert(budget)).to_entity()

    async def _upsert(self, budget: ApiKeyBudget) -> ApiKeyBudgetModel:
        row = await self._session.scalar(
            select(ApiKeyBudgetModel).where(ApiKeyBudgetModel.api_key_id == budget.api_key_id)
        )
        if row is None:
            row = ApiKeyBudgetModel(
                id=budget.id,
                api_key_id=budget.api_key_id,
                team_id=budget.team_id,
                limit_cost=budget.limit_cost,
                window=budget.window.value,
                mode=budget.mode.value,
            )
            self._session.add(row)
        else:
            row.limit_cost = budget.limit_cost
            row.window = budget.window.value
            row.mode = budget.mode.value
        await self._session.flush()
        return row

    async def remove(self, api_key_id: UUID) -> bool:
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            delete(ApiKeyBudgetModel).where(ApiKeyBudgetModel.api_key_id == api_key_id)
        )
        await self._session.commit()
        return bool(result.rowcount)
