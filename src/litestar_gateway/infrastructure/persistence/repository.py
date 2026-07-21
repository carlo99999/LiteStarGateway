"""SQLAlchemy adapter implementing the `APIKeyRepository` port."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import APIKey
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import (
    APIKeyModel,
    ServicePrincipalModel,
    UserModel,
)


class SQLAlchemyAPIKeyRepository:
    """Maps between `APIKey` domain entities and `APIKeyModel` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, key: APIKey) -> APIKey:
        model = APIKeyModel(
            id=key.id,
            team_id=key.team_id,
            created_by=key.created_by,
            name=key.name,
            prefix=key.prefix,
            key_hash=key.key_hash,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
            scope=key.scope.value,
            service_principal_id=key.service_principal_id,
            rate_limit_rpm=key.rate_limit_rpm,
            expires_at=key.expires_at,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, key_id: UUID) -> APIKey | None:
        model = await self._session.get(APIKeyModel, key_id)
        return model.to_entity() if model else None

    async def get_for_update(self, key_id: UUID) -> APIKey | None:
        model = await self._session.scalar(
            select(APIKeyModel).where(APIKeyModel.id == key_id).with_for_update()
        )
        return model.to_entity() if model else None

    async def get_by_hash(self, key_hash: str) -> APIKey | None:
        model = await self._session.scalar(
            select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
        )
        return model.to_entity() if model else None

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]:
        models = await self._session.scalars(
            select(APIKeyModel)
            .where(APIKeyModel.team_id == team_id)
            .order_by(APIKeyModel.created_at, APIKeyModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def list_by_creator(
        self, created_by: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]:
        models = await self._session.scalars(
            select(APIKeyModel)
            .where(APIKeyModel.created_by == created_by)
            .order_by(APIKeyModel.created_at, APIKeyModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def update(self, key: APIKey) -> APIKey:
        model = await self._session.get(APIKeyModel, key.id)
        if model is None:  # pragma: no cover - guarded by callers
            raise LookupError(f"APIKey {key.id} disappeared")
        model.revoked_at = key.revoked_at
        model.last_used_at = key.last_used_at
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def touch_last_used(self, key_id: UUID, last_used_at: datetime) -> bool:
        # PostgreSQL's CURRENT_TIMESTAMP/now() is fixed at transaction start,
        # which would let a revocation committed later in this auth transaction
        # look "future". statement_timestamp() linearizes this check at the
        # UPDATE statement; SQLite's CURRENT_TIMESTAMP already has that behavior.
        bind = self._session.get_bind()
        statement_now = (
            func.statement_timestamp()
            if bind.dialect.name == "postgresql"
            else func.current_timestamp()
        )
        # Any: async execute() is typed Result, but DML returns CursorResult.
        result: Any = await self._session.execute(
            update(APIKeyModel)
            .where(
                APIKeyModel.id == key_id,
                or_(
                    APIKeyModel.revoked_at.is_(None),
                    APIKeyModel.revoked_at > statement_now,
                ),
            )
            .values(last_used_at=last_used_at)
        )
        await self._session.commit()
        return result.rowcount == 1

    async def is_authenticatable(self, key_id: UUID) -> bool:
        bind = self._session.get_bind()
        statement_now = (
            func.statement_timestamp()
            if bind.dialect.name == "postgresql"
            else func.current_timestamp()
        )
        authenticated_id = await self._session.scalar(
            select(APIKeyModel.id)
            .join(UserModel, UserModel.id == APIKeyModel.created_by)
            .outerjoin(
                ServicePrincipalModel,
                ServicePrincipalModel.id == APIKeyModel.service_principal_id,
            )
            .where(
                APIKeyModel.id == key_id,
                or_(
                    APIKeyModel.revoked_at.is_(None),
                    APIKeyModel.revoked_at > statement_now,
                ),
                or_(
                    and_(
                        APIKeyModel.service_principal_id.is_(None),
                        UserModel.is_active.is_(True),
                    ),
                    and_(
                        APIKeyModel.service_principal_id.is_not(None),
                        ServicePrincipalModel.id.is_not(None),
                        ServicePrincipalModel.enabled.is_(True),
                    ),
                ),
            )
        )
        return authenticated_id is not None

    async def schedule_revocation(
        self,
        key_id: UUID,
        expected_revoked_at: datetime | None,
        revoked_at: datetime,
    ) -> bool:
        expected = (
            APIKeyModel.revoked_at.is_(None)
            if expected_revoked_at is None
            else APIKeyModel.revoked_at == expected_revoked_at
        )
        # Any: async execute() is typed Result, but DML returns CursorResult.
        result: Any = await self._session.execute(
            update(APIKeyModel)
            .where(APIKeyModel.id == key_id, expected)
            .values(revoked_at=revoked_at)
        )
        await self._session.flush()
        return result.rowcount == 1

    async def revoke_personal_keys_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        await self._session.execute(
            update(APIKeyModel)
            .where(
                APIKeyModel.created_by == user_id,
                APIKeyModel.service_principal_id.is_(None),
                or_(
                    APIKeyModel.revoked_at.is_(None),
                    APIKeyModel.revoked_at > revoked_at,
                ),
            )
            .values(revoked_at=revoked_at)
        )
        await self._session.flush()

    async def revoke_for_service_principal(
        self, service_principal_id: UUID, revoked_at: datetime
    ) -> None:
        await self._session.execute(
            update(APIKeyModel)
            .where(
                APIKeyModel.service_principal_id == service_principal_id,
            )
            .values(
                revoked_at=case(
                    (
                        or_(
                            APIKeyModel.revoked_at.is_(None),
                            APIKeyModel.revoked_at > revoked_at,
                        ),
                        revoked_at,
                    ),
                    else_=APIKeyModel.revoked_at,
                ),
                service_principal_id=None,
            )
        )
        await self._session.flush()
