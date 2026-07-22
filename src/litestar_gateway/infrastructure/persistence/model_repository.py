"""SQLAlchemy adapter implementing the `ModelRepository` port."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import Model, ModelGrant
from litestar_gateway.domain.exceptions import ModelNameExists, ModelNotFound, ModelShared
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.callable_alias_slots import (
    claim_direct,
    lock_resource_lifecycle,
    promote_direct,
    rename_direct,
    tombstone_grant,
    tombstone_resource,
    tombstone_resource_grants,
)
from litestar_gateway.infrastructure.persistence.orm import (
    CallableAliasRecord,
    ModelGrantRecord,
    ModelRecord,
)

_GLOBAL_SUFFIX = "-global"


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
            origin_team_id=model.origin_team_id,
        )
        try:
            self._session.add(record)
            await self._session.flush()
            await claim_direct(
                self._session, CallableKind.MODEL, model.id, model.team_id, model.name
            )
            await self._session.commit()
        except IntegrityError as exc:
            # Loser of a concurrent create with the same name (per team, or global):
            # the service's pre-check passed for both, the unique constraint (or the
            # partial global index) catches the race. Translate to the domain 409.
            await self._session.rollback()
            raise ModelNameExists(model.name) from exc
        await self._session.refresh(record)
        return record.to_entity()

    async def get(self, model_id: UUID) -> Model | None:
        record = await self._session.get(ModelRecord, model_id)
        return record.to_entity() if record else None

    async def get_global(self, model_id: UUID) -> Model | None:
        record = await self._session.scalar(
            select(ModelRecord).where(ModelRecord.id == model_id, ModelRecord.team_id.is_(None))
        )
        return record.to_entity() if record else None

    async def get_by_name(self, team_id: UUID | None, name: str) -> Model | None:
        # 1. The team's own model always wins.
        own = await self._session.scalar(
            select(ModelRecord).where(ModelRecord.team_id == team_id, ModelRecord.name == name)
        )
        if own is not None:
            return own.to_entity()
        # 2. A model extended to this team under this alias.
        grant = await self._session.scalar(
            select(ModelGrantRecord).where(
                ModelGrantRecord.team_id == team_id, ModelGrantRecord.alias == name
            )
        )
        if grant is not None:
            source = await self._session.get(ModelRecord, grant.model_id)
            return source.to_entity() if source else None
        # 3. A global model by its plain name — reachable here only because the
        #    team has no own model of that name (step 1 missed).
        glob = await self._session.scalar(
            select(ModelRecord).where(ModelRecord.team_id.is_(None), ModelRecord.name == name)
        )
        if glob is not None:
            return glob.to_entity()
        # 4. The disambiguated `<base>-global`, valid only when the team's own
        #    `<base>` shadows a global of that base name.
        if name.endswith(_GLOBAL_SUFFIX):
            base = name[: -len(_GLOBAL_SUFFIX)]
            shadows = await self._session.scalar(
                select(ModelRecord.id)
                .where(ModelRecord.team_id == team_id, ModelRecord.name == base)
                .limit(1)
            )
            if shadows is not None:
                glob = await self._session.scalar(
                    select(ModelRecord).where(
                        ModelRecord.team_id.is_(None), ModelRecord.name == base
                    )
                )
                if glob is not None:
                    return glob.to_entity()
        return None

    async def name_taken_in_team(self, team_id: UUID, name: str) -> bool:
        own = await self._session.scalar(
            select(ModelRecord.id)
            .where(ModelRecord.team_id == team_id, ModelRecord.name == name)
            .limit(1)
        )
        if own is not None:
            return True
        grant = await self._session.scalar(
            select(ModelGrantRecord.id)
            .where(ModelGrantRecord.team_id == team_id, ModelGrantRecord.alias == name)
            .limit(1)
        )
        return grant is not None

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]:
        records = await self._session.scalars(
            select(ModelRecord)
            .where(ModelRecord.team_id == team_id)
            .order_by(ModelRecord.created_at, ModelRecord.id)
            .limit(limit)
            .offset(offset)
        )
        return [r.to_entity() for r in records]

    async def list_global(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Model]:
        records = await self._session.scalars(
            select(ModelRecord)
            .where(ModelRecord.team_id.is_(None))
            .order_by(ModelRecord.created_at, ModelRecord.id)
            .limit(limit)
            .offset(offset)
        )
        return [r.to_entity() for r in records]

    async def all_global(self) -> list[Model]:
        records = await self._session.scalars(
            select(ModelRecord)
            .where(ModelRecord.team_id.is_(None))
            .order_by(ModelRecord.created_at, ModelRecord.id)
        )
        return [r.to_entity() for r in records]

    async def update(self, model: Model) -> Model:
        record = await lock_resource_lifecycle(self._session, CallableKind.MODEL, model.id)
        if not isinstance(record, ModelRecord) or record.team_id != model.team_id:
            raise ModelNotFound(str(model.id))
        binding = await self._session.scalar(
            select(CallableAliasRecord).where(
                CallableAliasRecord.model_id == model.id,
                CallableAliasRecord.model_grant_id.is_(None),
            )
        )
        old_name = record.name
        try:
            self._apply_update(record, model)
            if binding is not None and old_name != model.name:
                await rename_direct(
                    self._session,
                    CallableKind.MODEL,
                    model.id,
                    model.team_id,
                    old_name,
                    model.name,
                )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ModelNameExists(model.name) from exc
        await self._session.refresh(record)
        return record.to_entity()

    @staticmethod
    def _apply_update(record: ModelRecord, model: Model) -> None:
        record.team_id = model.team_id
        record.origin_team_id = model.origin_team_id
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

    async def update_global(self, model: Model) -> Model | None:
        record = await lock_resource_lifecycle(self._session, CallableKind.MODEL, model.id)
        if not isinstance(record, ModelRecord) or record.team_id is not None:
            return None
        binding = await self._session.scalar(
            select(CallableAliasRecord).where(
                CallableAliasRecord.model_id == model.id,
                CallableAliasRecord.model_grant_id.is_(None),
            )
        )
        old_name = record.name
        try:
            self._apply_update(record, model)
            if binding is not None and old_name != model.name:
                await rename_direct(
                    self._session,
                    CallableKind.MODEL,
                    model.id,
                    None,
                    old_name,
                    model.name,
                )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ModelNameExists(model.name) from exc
        await self._session.refresh(record)
        return record.to_entity()

    async def remove(self, model_id: UUID) -> None:
        record = await lock_resource_lifecycle(self._session, CallableKind.MODEL, model_id)
        if not isinstance(record, ModelRecord) or record.team_id is None:
            raise ModelNotFound(str(model_id))
        if await self._session.scalar(
            select(ModelGrantRecord.id).where(ModelGrantRecord.model_id == model_id).limit(1)
        ):
            raise ModelShared("revoke every model grant before deleting the source model")
        await tombstone_resource(self._session, CallableKind.MODEL, model_id)
        await self._session.execute(delete(ModelRecord).where(ModelRecord.id == model_id))
        await self._session.commit()

    async def remove_global(self, model_id: UUID) -> bool:
        record = await lock_resource_lifecycle(self._session, CallableKind.MODEL, model_id)
        if not isinstance(record, ModelRecord) or record.team_id is not None:
            return False
        if await self._session.scalar(
            select(ModelGrantRecord.id).where(ModelGrantRecord.model_id == model_id).limit(1)
        ):
            raise ModelShared("revoke every model grant before deleting the global model")
        await tombstone_resource(self._session, CallableKind.MODEL, model_id)
        removed_id = await self._session.scalar(
            delete(ModelRecord)
            .where(ModelRecord.id == model_id, ModelRecord.team_id.is_(None))
            .returning(ModelRecord.id)
        )
        await self._session.commit()
        return removed_id is not None

    async def promote_to_global(self, model: Model) -> Model:
        """Promote ownership and remove all grants in one transaction."""
        record = await lock_resource_lifecycle(self._session, CallableKind.MODEL, model.id)
        if not isinstance(record, ModelRecord):
            raise ModelNotFound(str(model.id))
        if record.team_id is None:
            return record.to_entity()
        origin_team_id = record.team_id
        canonical_alias = record.name
        try:
            await tombstone_resource_grants(
                self._session, CallableKind.MODEL, model.id, canonical_alias
            )
            await self._session.execute(
                delete(ModelGrantRecord).where(ModelGrantRecord.model_id == model.id)
            )
            record.team_id = None
            record.origin_team_id = origin_team_id
            await promote_direct(self._session, CallableKind.MODEL, model.id, canonical_alias)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ModelNameExists(canonical_alias) from exc
        await self._session.refresh(record)
        return record.to_entity()

    async def exists_for_credential(self, credential_id: UUID) -> bool:
        record = await self._session.scalar(
            select(ModelRecord.id).where(ModelRecord.credential_id == credential_id).limit(1)
        )
        return record is not None

    # Grants (extending a team model to other teams).

    async def add_grant(self, grant: ModelGrant) -> ModelGrant:
        return (await self.add_grants([grant]))[0]

    async def add_grants(self, grants: list[ModelGrant]) -> list[ModelGrant]:
        if not grants:
            return []
        resource_ids = {grant.model_id for grant in grants}
        if len(resource_ids) != 1:
            raise ValueError("one add_grants call must target exactly one model")
        model_id = next(iter(resource_ids))
        source = await lock_resource_lifecycle(self._session, CallableKind.MODEL, model_id)
        if not isinstance(source, ModelRecord) or source.team_id is None:
            raise ModelNameExists(grants[0].alias)
        records = [
            ModelGrantRecord(
                id=grant.id,
                model_id=grant.model_id,
                team_id=grant.team_id,
                alias=grant.alias,
            )
            for grant in grants
        ]
        try:
            self._session.add_all(records)
            await self._session.flush()
            self._session.add_all(
                [
                    CallableAliasRecord(
                        id=uuid4(),
                        team_id=grant.team_id,
                        alias=grant.alias,
                        model_id=grant.model_id,
                        model_grant_id=grant.id,
                    )
                    for grant in grants
                ]
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # Either the alias collides in the target team, or the model is
            # already extended to it — both surface as a 409.
            raise ModelNameExists(grants[0].alias) from exc
        for record in records:
            await self._session.refresh(record)
        return [record.to_entity() for record in records]

    async def get_grant(self, grant_id: UUID) -> ModelGrant | None:
        record = await self._session.get(ModelGrantRecord, grant_id)
        return record.to_entity() if record else None

    async def remove_grant(self, grant_id: UUID) -> None:
        grant = await self._session.scalar(
            select(ModelGrantRecord).where(ModelGrantRecord.id == grant_id)
        )
        if grant is None:
            return
        source = await lock_resource_lifecycle(self._session, CallableKind.MODEL, grant.model_id)
        if not isinstance(source, ModelRecord):
            return
        # Recheck after the lifecycle lock: a source deletion that won the race
        # may have cascaded this grant while we waited.
        if await self._session.get(ModelGrantRecord, grant_id, populate_existing=True) is None:
            return
        await tombstone_grant(self._session, CallableKind.MODEL, grant_id)
        await self._session.execute(delete(ModelGrantRecord).where(ModelGrantRecord.id == grant_id))
        await self._session.commit()

    async def list_grants_for_model(self, model_id: UUID) -> list[ModelGrant]:
        records = await self._session.scalars(
            select(ModelGrantRecord)
            .where(ModelGrantRecord.model_id == model_id)
            .order_by(ModelGrantRecord.created_at, ModelGrantRecord.id)
        )
        return [r.to_entity() for r in records]

    async def list_grants_for_team(self, team_id: UUID) -> list[ModelGrant]:
        records = await self._session.scalars(
            select(ModelGrantRecord)
            .where(ModelGrantRecord.team_id == team_id)
            .order_by(ModelGrantRecord.created_at, ModelGrantRecord.id)
        )
        return [r.to_entity() for r in records]
