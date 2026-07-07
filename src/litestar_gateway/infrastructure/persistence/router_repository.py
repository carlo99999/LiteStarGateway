"""SQLAlchemy adapters for the `RouterRepository` and `RoutingDecisionLog` ports."""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.exceptions import RouterNameExists, RouterNotFound
from litestar_gateway.domain.routing import RouterConfig, RoutingDecisionRecord
from litestar_gateway.infrastructure.persistence.orm import RouterModel, RoutingDecisionModel


def _candidates_json(router: RouterConfig) -> list[dict]:
    return [dataclasses.asdict(candidate) for candidate in router.candidates]


class SQLAlchemyRouterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, router: RouterConfig) -> RouterConfig:
        model = RouterModel(
            id=router.id,
            team_id=router.team_id,
            name=router.name,
            candidates=_candidates_json(router),
            default_model=router.default_model,
            strategy=router.strategy,
            strategy_config=router.strategy_config,
            shadow_strategy=router.shadow_strategy,
            enabled=router.enabled,
        )
        self._session.add(model)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(router.name) from exc
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, team_id: UUID, router_id: UUID) -> RouterConfig | None:
        model = await self._session.get(RouterModel, router_id)
        if model is None or model.team_id != team_id:
            return None
        return model.to_entity()

    async def get_by_name(self, team_id: UUID, name: str) -> RouterConfig | None:
        model = await self._session.scalar(
            select(RouterModel).where(RouterModel.team_id == team_id, RouterModel.name == name)
        )
        return model.to_entity() if model else None

    async def list_by_team(self, team_id: UUID) -> list[RouterConfig]:
        result = await self._session.scalars(
            select(RouterModel).where(RouterModel.team_id == team_id).order_by(RouterModel.name)
        )
        return [model.to_entity() for model in result]

    async def update(self, router: RouterConfig) -> RouterConfig:
        model = await self._session.get(RouterModel, router.id)
        if model is None or model.team_id != router.team_id:
            raise RouterNotFound(str(router.id))
        model.name = router.name
        model.candidates = _candidates_json(router)
        model.default_model = router.default_model
        model.strategy = router.strategy
        model.strategy_config = router.strategy_config
        model.shadow_strategy = router.shadow_strategy
        model.enabled = router.enabled
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(router.name) from exc
        await self._session.refresh(model)
        return model.to_entity()

    async def delete(self, team_id: UUID, router_id: UUID) -> bool:
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            delete(RouterModel).where(RouterModel.id == router_id, RouterModel.team_id == team_id)
        )
        await self._session.commit()
        return bool(result.rowcount)


class SQLAlchemyRoutingDecisionLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, decision: RoutingDecisionRecord) -> None:
        self._session.add(
            RoutingDecisionModel(
                id=decision.id,
                team_id=decision.team_id,
                router_name=decision.router_name,
                strategy=decision.strategy,
                chosen_model=decision.chosen_model,
                tier=decision.tier,
                score=decision.score,
                signals=list(decision.signals),
                decision_ms=decision.decision_ms,
                is_shadow=decision.is_shadow,
                fallback_used=decision.fallback_used,
                api_key_id=decision.api_key_id,
            )
        )
        await self._session.commit()
