"""SQLAlchemy adapter implementing the `UserRepository` port."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import User
from litestar_gateway.domain.exceptions import EmailAlreadyRegistered, UserNotFound
from litestar_gateway.infrastructure.persistence.orm import UserModel


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
            sso_subject=user.sso_subject,
            is_active=user.is_active,
            external_id=user.external_id,
        )
        self._session.add(model)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            # A unique constraint (email or sso_subject) already holds — surface a
            # domain error so callers can handle the conflict / concurrent insert.
            await self._session.rollback()
            raise EmailAlreadyRegistered(user.email) from exc
        await self._session.refresh(model)
        return model.to_entity()

    async def set_active(self, user_id: UUID, is_active: bool) -> None:
        # Disabling revokes existing sessions too (bump token_version), so the
        # account is locked out immediately, not just barred from new logins.
        values: dict[str, object] = {"is_active": is_active}
        if not is_active:
            values["token_version"] = UserModel.token_version + 1
        await self._session.execute(
            update(UserModel).where(UserModel.id == user_id).values(**values)
        )
        await self._session.commit()

    async def set_admin(self, user_id: UUID, is_admin: bool) -> None:
        # The platform role is read live from the DB on every request (see
        # provide_current_admin) and is not carried in the JWT, so the change takes
        # effect on the target's next call — no token_version bump needed.
        await self._session.execute(
            update(UserModel).where(UserModel.id == user_id).values(is_admin=is_admin)
        )
        await self._session.commit()

    async def increment_token_version(self, user_id: UUID) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(token_version=UserModel.token_version + 1)
        )
        await self._session.commit()

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        # Bump token_version in the same update so a reset revokes existing JWTs.
        # Stages only (no commit): committed by the service together with the
        # reset-token consumption (one unit of work).
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(password_hash=password_hash, token_version=UserModel.token_version + 1)
        )

    async def register_failed_login(self, user_id: UUID) -> int:
        # Atomic in-database increment (no read-modify-write), so concurrent
        # failed attempts across workers are all counted.
        count = await self._session.scalar(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(failed_login_attempts=UserModel.failed_login_attempts + 1)
            .returning(UserModel.failed_login_attempts)
        )
        await self._session.commit()
        return count or 0

    async def set_login_lock(
        self, user_id: UUID, locked_until: datetime, *, reset_cycles: bool
    ) -> None:
        # Increment lockout_cycles in-database (not from a stale read), so two
        # concurrent threshold-crossing failures can't both write the same
        # cycles+1 and lose an escalation step (L20). A decayed lock restarts the
        # curve at 1.
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(
                locked_until=locked_until,
                failed_login_attempts=0,
                lockout_cycles=1 if reset_cycles else UserModel.lockout_cycles + 1,
            )
        )
        await self._session.commit()

    async def clear_login_failures(self, user_id: UUID) -> None:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(failed_login_attempts=0, locked_until=None, lockout_cycles=0)
        )
        await self._session.commit()

    async def get(self, user_id: UUID) -> User | None:
        model = await self._session.get(UserModel, user_id)
        return model.to_entity() if model else None

    async def get_by_email(self, email: str) -> User | None:
        model = await self._session.scalar(select(UserModel).where(UserModel.email == email))
        return model.to_entity() if model else None

    async def get_by_sso_subject(self, subject: str) -> User | None:
        model = await self._session.scalar(
            select(UserModel).where(UserModel.sso_subject == subject)
        )
        return model.to_entity() if model else None

    async def get_by_external_id(self, external_id: str) -> User | None:
        model = await self._session.scalar(
            select(UserModel).where(UserModel.external_id == external_id)
        )
        return model.to_entity() if model else None

    async def list(self, *, offset: int, limit: int) -> list[User]:
        result = await self._session.scalars(
            select(UserModel)
            .order_by(UserModel.created_at, UserModel.id)
            .offset(offset)
            .limit(limit)
        )
        return [model.to_entity() for model in result]

    async def update_scim_identity(
        self, user_id: UUID, email: str, external_id: str | None
    ) -> User:
        try:
            await self._session.execute(
                update(UserModel)
                .where(UserModel.id == user_id)
                .values(email=email, external_id=external_id)
            )
            await self._session.commit()
        except IntegrityError as exc:
            # email or external_id already belongs to another account.
            await self._session.rollback()
            raise EmailAlreadyRegistered(email) from exc
        model = await self._session.get(UserModel, user_id)
        if model is None:
            raise UserNotFound(str(user_id))
        return model.to_entity()

    async def bind_sso(self, user_id: UUID, sso_subject: str, is_admin: bool) -> User:
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(sso_subject=sso_subject, is_admin=is_admin)
        )
        await self._session.commit()
        model = await self._session.get(UserModel, user_id)
        if model is None:
            raise UserNotFound(str(user_id))
        return model.to_entity()

    async def count(self) -> int:
        result = await self._session.scalar(select(func.count()).select_from(UserModel))
        return result or 0
