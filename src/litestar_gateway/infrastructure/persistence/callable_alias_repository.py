"""SQLAlchemy adapter for the shared callable-alias registry."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import (
    CallableAliasBinding,
    CallableKind,
    CallableOrigin,
)
from litestar_gateway.infrastructure.persistence.orm import (
    CallableAliasRecord,
    ModelRecord,
    RouterModel,
)


class SQLAlchemyCallableAliasRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, team_id: UUID | None) -> tuple[list[CallableAliasBinding], set[str]]:
        scope = (
            CallableAliasRecord.team_id.is_(None)
            if team_id is None
            else or_(CallableAliasRecord.team_id == team_id, CallableAliasRecord.team_id.is_(None))
        )
        rows = await self._session.execute(
            select(
                CallableAliasRecord,
                ModelRecord.team_id,
                ModelRecord.origin_team_id,
                RouterModel.team_id,
                RouterModel.origin_team_id,
            )
            .outerjoin(ModelRecord, ModelRecord.id == CallableAliasRecord.model_id)
            .outerjoin(RouterModel, RouterModel.id == CallableAliasRecord.router_id)
            .where(scope)
            .order_by(CallableAliasRecord.alias, CallableAliasRecord.id)
        )
        bindings: list[CallableAliasBinding] = []
        reserved: set[str] = set()
        for row, model_team_id, model_origin_team_id, router_team_id, router_origin_team_id in rows:
            if row.unavailable:
                reserved.add(row.alias)
                continue
            if row.model_id is not None:
                kind = CallableKind.MODEL
                resource_id = row.model_id
                has_grant = row.model_grant_id is not None
                resource_team_id = model_team_id
                resource_origin_team_id = model_origin_team_id
            else:
                kind = CallableKind.ROUTER
                assert row.router_id is not None
                resource_id = row.router_id
                has_grant = row.router_grant_id is not None
                resource_team_id = router_team_id
                resource_origin_team_id = router_origin_team_id
            origin = (
                CallableOrigin.GLOBAL
                if row.team_id is None
                else CallableOrigin.EXTENDED
                if has_grant
                else CallableOrigin.OWN
            )
            source_team_id = (
                resource_origin_team_id if origin is CallableOrigin.GLOBAL else resource_team_id
            )
            bindings.append(
                CallableAliasBinding(
                    id=row.id,
                    team_id=row.team_id,
                    alias=row.alias,
                    kind=kind,
                    resource_id=resource_id,
                    origin=origin,
                    source_team_id=source_team_id,
                )
            )
        return bindings, reserved

    async def list_explicit(self, team_id: UUID | None) -> list[CallableAliasBinding]:
        bindings, _ = await self.snapshot(team_id)
        return bindings

    async def list_reserved(self, team_id: UUID | None) -> set[str]:
        _, reserved = await self.snapshot(team_id)
        return reserved

    async def slot_reserved(self, team_id: UUID | None, alias: str) -> bool:
        scope = (
            CallableAliasRecord.team_id.is_(None)
            if team_id is None
            else CallableAliasRecord.team_id == team_id
        )
        row_id = await self._session.scalar(
            select(CallableAliasRecord.id).where(scope, CallableAliasRecord.alias == alias).limit(1)
        )
        return row_id is not None
