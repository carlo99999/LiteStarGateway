"""Database-level concurrency tests for platform-admin deletion invariants."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from litestar.testing import AsyncTestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.app import create_app
from litestar_gateway.application.user_service import UserService
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.exceptions import PermissionDenied
from litestar_gateway.infrastructure.persistence.invite_repository import (
    SQLAlchemyInviteRepository,
)
from litestar_gateway.infrastructure.persistence.password_reset_repository import (
    SQLAlchemyPasswordResetRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def _two_party_barrier(
    original: Callable[..., Awaitable[list[User]]],
) -> Callable[..., Awaitable[list[User]]]:
    arrived = 0
    ready = asyncio.Event()

    async def wrapped(*args: object, **kwargs: object) -> list[User]:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return await original(*args, **kwargs)

    return wrapped


def _service(session: AsyncSession) -> UserService:
    return UserService(
        users=SQLAlchemyUserRepository(session),
        invites=SQLAlchemyInviteRepository(session),
        password_resets=SQLAlchemyPasswordResetRepository(session),
        transaction=session,
    )


@pytest.mark.parametrize("second_operation", ["delete", "demote", "disable"])
async def test_concurrent_admin_changes_preserve_one_active_admin(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    second_operation: str,
) -> None:
    settings = Settings(
        database_url=database_url,
        admin_email="admin-a@example.com",
        master_key="master-secret",  # pragma: allowlist secret
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)):
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as setup_session:
                users = SQLAlchemyUserRepository(setup_session)
                admin_a = await users.get_by_email("admin-a@example.com")
                assert admin_a is not None
                admin_b = await users.add(
                    User(
                        id=uuid4(),
                        email="admin-b@example.com",
                        password_hash="unused",  # pragma: allowlist secret
                        is_admin=True,
                        created_at=datetime.now(UTC),
                    )
                )

            monkeypatch.setattr(
                SQLAlchemyUserRepository,
                "lock_platform_admins",
                _two_party_barrier(SQLAlchemyUserRepository.lock_platform_admins),
            )
            async with sessions() as first_session, sessions() as second_session:
                first = _service(first_session).delete_user(admin_a, admin_b.id)
                second_service = _service(second_session)
                if second_operation == "delete":
                    second = second_service.delete_user(admin_b, admin_a.id)
                elif second_operation == "demote":
                    second = second_service.set_user_admin(admin_b, admin_a.id, False)
                else:
                    second = second_service.set_user_active(admin_b, admin_a.id, False)
                results = await asyncio.wait_for(
                    asyncio.gather(
                        first,
                        second,
                        return_exceptions=True,
                    ),
                    timeout=10,
                )
            monkeypatch.undo()

            assert sum(isinstance(result, User) for result in results) == 1
            assert sum(isinstance(result, PermissionDenied) for result in results) == 1
            async with sessions() as verify_session:
                remaining = await SQLAlchemyUserRepository(verify_session).list(offset=0, limit=10)
            assert sum(user.is_admin and user.is_active for user in remaining) == 1
        finally:
            monkeypatch.undo()
            await engine.dispose()
