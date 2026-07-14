"""SQLAlchemy adapter implementing the `OrganizationRepository` port."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Organization
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import OrganizationModel


class SQLAlchemyOrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, organization: Organization) -> Organization:
        model = OrganizationModel(
            id=organization.id,
            name=organization.name,
            description=organization.description,
            tags=list(organization.tags),
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, organization_id: UUID) -> Organization | None:
        model = await self._session.get(OrganizationModel, organization_id)
        return model.to_entity() if model else None

    async def list(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Organization]:
        models = await self._session.scalars(
            select(OrganizationModel)
            .order_by(OrganizationModel.created_at, OrganizationModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def update(
        self, organization_id: UUID, name: str, description: str | None, tags: Sequence[str]
    ) -> Organization | None:
        model = await self._session.get(OrganizationModel, organization_id)
        if model is None:
            return None
        model.name = name
        model.description = description
        model.tags = list(tags)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def delete(self, organization_id: UUID) -> bool:
        model = await self._session.get(OrganizationModel, organization_id)
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.commit()
        return True
