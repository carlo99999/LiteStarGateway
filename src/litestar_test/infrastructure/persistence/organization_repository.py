"""SQLAlchemy adapter implementing the `OrganizationRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import Organization
from litestar_test.infrastructure.persistence.orm import OrganizationModel


class SQLAlchemyOrganizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, organization: Organization) -> Organization:
        model = OrganizationModel(id=organization.id, name=organization.name)
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, organization_id: UUID) -> Organization | None:
        model = await self._session.get(OrganizationModel, organization_id)
        return model.to_entity() if model else None

    async def list(self) -> list[Organization]:
        models = await self._session.scalars(
            select(OrganizationModel).order_by(OrganizationModel.created_at)
        )
        return [m.to_entity() for m in models]
