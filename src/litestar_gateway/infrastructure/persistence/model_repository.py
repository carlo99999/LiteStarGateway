"""SQLAlchemy adapter implementing the `ModelRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import ModelRecord


class SQLAlchemyModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, model: Model) -> Model:
        record = ModelRecord(
            id=model.id,
            team_id=model.team_id,
            name=model.name,
            provider=model.provider.value,
            credential_id=model.credential_id,
            type=model.type.value,
            provider_model_id=model.provider_model_id,
            params=model.params,
            params_enforced=model.params_enforced,
            max_output_tokens=model.max_output_tokens,
            api_version=model.api_version,
            input_cost_per_token=model.input_cost_per_token,
            output_cost_per_token=model.output_cost_per_token,
            enabled=model.enabled,
        )
        self._session.add(record)
        await self._session.commit()
        await self._session.refresh(record)
        return record.to_entity()

    async def get(self, model_id: UUID) -> Model | None:
        record = await self._session.get(ModelRecord, model_id)
        return record.to_entity() if record else None

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        record = await self._session.scalar(
            select(ModelRecord).where(ModelRecord.team_id == team_id, ModelRecord.name == name)
        )
        return record.to_entity() if record else None

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]:
        records = await self._session.scalars(
            select(ModelRecord)
            .where(ModelRecord.team_id == team_id)
            .order_by(ModelRecord.created_at)
            .limit(limit)
            .offset(offset)
        )
        return [r.to_entity() for r in records]

    async def update(self, model: Model) -> Model:
        record = await self._session.get(ModelRecord, model.id)
        if record is None:  # pragma: no cover - guarded by callers
            raise LookupError(f"Model {model.id} disappeared")
        record.name = model.name
        record.type = model.type.value
        record.provider_model_id = model.provider_model_id
        record.params = model.params
        record.params_enforced = model.params_enforced
        record.max_output_tokens = model.max_output_tokens
        record.api_version = model.api_version
        record.input_cost_per_token = model.input_cost_per_token
        record.output_cost_per_token = model.output_cost_per_token
        record.enabled = model.enabled
        await self._session.commit()
        await self._session.refresh(record)
        return record.to_entity()

    async def remove(self, model_id: UUID) -> None:
        await self._session.execute(delete(ModelRecord).where(ModelRecord.id == model_id))
        await self._session.commit()

    async def exists_for_credential(self, credential_id: UUID) -> bool:
        record = await self._session.scalar(
            select(ModelRecord.id).where(ModelRecord.credential_id == credential_id).limit(1)
        )
        return record is not None
