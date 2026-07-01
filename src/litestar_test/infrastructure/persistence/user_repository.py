"""SQLAlchemy adapter implementing the `UserRepository` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import User
from litestar_test.infrastructure.persistence.orm import UserModel


class SQLAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> User:
        model = UserModel(
            id=user.id,
            email=user.email,
            password_hash=user.password_hash,
            is_admin=user.is_admin,
            token_version=user.token_version,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def increment_token_version(self, user_id: UUID) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(token_version=UserModel.token_version + 1)
        )
        await self._session.commit()

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        # Bump token_version in the same update so a reset revokes existing JWTs.
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(password_hash=password_hash, token_version=UserModel.token_version + 1)
        )
        await self._session.commit()

    async def get(self, user_id: UUID) -> User | None:
        model = await self._session.get(UserModel, user_id)
        return model.to_entity() if model else None

    async def get_by_email(self, email: str) -> User | None:
        model = await self._session.scalar(select(UserModel).where(UserModel.email == email))
        return model.to_entity() if model else None

    async def count(self) -> int:
        result = await self._session.scalar(select(func.count()).select_from(UserModel))
        return result or 0
