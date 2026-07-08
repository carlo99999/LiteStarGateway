"""SQLAlchemy adapter implementing the `ServicePrincipalRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import ServicePrincipal
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import ServicePrincipalModel


class SQLAlchemyServicePrincipalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, sp: ServicePrincipal) -> ServicePrincipal:
        model = ServicePrincipalModel(
            id=sp.id, team_id=sp.team_id, name=sp.name, enabled=sp.enabled
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, sp_id: UUID) -> ServicePrincipal | None:
        model = await self._session.get(ServicePrincipalModel, sp_id)
        return model.to_entity() if model else None

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[ServicePrincipal]:
        models = await self._session.scalars(
            select(ServicePrincipalModel)
            .where(ServicePrincipalModel.team_id == team_id)
            .order_by(ServicePrincipalModel.created_at, ServicePrincipalModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def set_enabled(self, sp_id: UUID, enabled: bool) -> ServicePrincipal:
        model = await self._session.get(ServicePrincipalModel, sp_id)
        if model is None:  # pragma: no cover - guarded by the service
            raise LookupError(f"ServicePrincipal {sp_id} disappeared")
        model.enabled = enabled
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def remove(self, sp_id: UUID) -> None:
        await self._session.execute(
            delete(ServicePrincipalModel).where(ServicePrincipalModel.id == sp_id)
        )
        await self._session.commit()
