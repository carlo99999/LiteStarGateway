"""SQLAlchemy adapters for the `RouterRepository` and `RoutingDecisionLog` ports."""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
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
                chosen_input_cost=decision.chosen_input_cost,
                chosen_output_cost=decision.chosen_output_cost,
                alt_input_cost=decision.alt_input_cost,
                alt_output_cost=decision.alt_output_cost,
                prompt_tokens=decision.prompt_tokens,
                completion_tokens=decision.completion_tokens,
                user_text=decision.user_text,
                system_prompt=decision.system_prompt,
            )
        )
        await self._session.commit()

    async def update_usage(
        self, decision_id: UUID, prompt_tokens: int, completion_tokens: int
    ) -> None:
        await self._session.execute(
            update(RoutingDecisionModel)
            .where(RoutingDecisionModel.id == decision_id)
            .values(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        )
        await self._session.commit()

    async def list_decisions(
        self,
        team_id: UUID,
        router_name: str,
        *,
        strategy: str | None = None,
        chosen_model: str | None = None,
        is_shadow: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RoutingDecisionRecord]:
        stmt = select(RoutingDecisionModel).where(
            RoutingDecisionModel.team_id == team_id,
            RoutingDecisionModel.router_name == router_name,
        )
        if strategy is not None:
            stmt = stmt.where(RoutingDecisionModel.strategy == strategy)
        if chosen_model is not None:
            stmt = stmt.where(RoutingDecisionModel.chosen_model == chosen_model)
        if is_shadow is not None:
            stmt = stmt.where(RoutingDecisionModel.is_shadow == is_shadow)
        stmt = stmt.order_by(RoutingDecisionModel.created_at.desc()).offset(offset).limit(limit)
        return [model.to_entity() for model in await self._session.scalars(stmt)]

    async def distribution(
        self, team_id: UUID, router_name: str
    ) -> list[tuple[str, str | None, bool, int]]:
        stmt = (
            select(
                RoutingDecisionModel.chosen_model,
                RoutingDecisionModel.tier,
                RoutingDecisionModel.is_shadow,
                func.count(),
            )
            .where(
                RoutingDecisionModel.team_id == team_id,
                RoutingDecisionModel.router_name == router_name,
            )
            .group_by(
                RoutingDecisionModel.chosen_model,
                RoutingDecisionModel.tier,
                RoutingDecisionModel.is_shadow,
            )
        )
        return [tuple(row) for row in await self._session.execute(stmt)]

    async def savings(self, team_id: UUID, router_name: str) -> tuple[float, int, int]:
        # Σ over non-shadow decisions with usage AND both cost profiles:
        # (alt − chosen) unit cost × the request's actual tokens.
        base = (
            RoutingDecisionModel.team_id == team_id,
            RoutingDecisionModel.router_name == router_name,
            RoutingDecisionModel.is_shadow.is_(False),
        )
        counted = (
            *base,
            RoutingDecisionModel.prompt_tokens.is_not(None),
            RoutingDecisionModel.completion_tokens.is_not(None),
            RoutingDecisionModel.alt_input_cost.is_not(None),
            RoutingDecisionModel.chosen_input_cost.is_not(None),
            RoutingDecisionModel.alt_output_cost.is_not(None),
            RoutingDecisionModel.chosen_output_cost.is_not(None),
        )
        total = await self._session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        (
                            RoutingDecisionModel.alt_input_cost
                            - RoutingDecisionModel.chosen_input_cost
                        )
                        * RoutingDecisionModel.prompt_tokens
                        + (
                            RoutingDecisionModel.alt_output_cost
                            - RoutingDecisionModel.chosen_output_cost
                        )
                        * RoutingDecisionModel.completion_tokens
                    ),
                    0.0,
                )
            ).where(*counted)
        )
        counted_n = await self._session.scalar(select(func.count()).where(*counted)) or 0
        all_n = await self._session.scalar(select(func.count()).where(*base)) or 0
        return float(total or 0.0), int(counted_n), int(all_n - counted_n)
