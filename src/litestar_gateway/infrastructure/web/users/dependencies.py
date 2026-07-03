"""Dependency wiring: build the UserService from a DB session."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.user_service import UserService
from litestar_test.infrastructure.persistence.invite_repository import (
    SQLAlchemyInviteRepository,
)
from litestar_test.infrastructure.persistence.password_reset_repository import (
    SQLAlchemyPasswordResetRepository,
)
from litestar_test.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def provide_user_service(db_session: NamedDependency[AsyncSession]) -> UserService:
    return UserService(
        users=SQLAlchemyUserRepository(db_session),
        invites=SQLAlchemyInviteRepository(db_session),
        password_resets=SQLAlchemyPasswordResetRepository(db_session),
    )
