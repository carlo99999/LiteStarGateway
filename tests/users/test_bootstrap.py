"""UserService.ensure_admin: bootstrap the first platform admin from MASTER_KEY."""

from __future__ import annotations

import pytest
from conftest import (
    FakeInviteRepository,
    FakePasswordResetRepository,
    FakeTransaction,
    FakeUserRepository,
)

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.exceptions import EmailAlreadyRegistered, MasterKeyMissing


async def test_ensure_admin_raises_when_empty_and_no_master_key(
    service: UserService,
) -> None:
    with pytest.raises(MasterKeyMissing):
        await service.ensure_admin("admin@example.com", master_key=None)


async def test_ensure_admin_creates_admin_with_master_key(service: UserService) -> None:
    await service.ensure_admin("admin@example.com", master_key="secret")
    admin = await service._users.get_by_email("admin@example.com")
    assert admin is not None
    assert admin.is_admin is True
    assert admin.password_hash != "secret"  # stored hashed


async def test_ensure_admin_is_idempotent_when_users_exist(
    service: UserService,
) -> None:
    await service.ensure_admin("admin@example.com", master_key="secret")
    # A second call with no key must NOT raise, because users already exist.
    await service.ensure_admin("admin@example.com", master_key=None)
    assert await service._users.count() == 1


async def test_ensure_admin_tolerates_concurrent_replica_bootstrap() -> None:
    """Two replicas racing ensure_admin on an empty table: the loser's add() hits
    the unique email constraint (EmailAlreadyRegistered) and must not crash startup."""

    class RacedUserRepository(FakeUserRepository):
        async def count(self) -> int:
            return 0  # both replicas observed an empty table

        async def add(self, user: User) -> User:
            if user.email in self._by_email:
                raise EmailAlreadyRegistered(user.email)
            return await super().add(user)

    users = RacedUserRepository()
    service = UserService(
        transaction=FakeTransaction(),
        users=users,
        invites=FakeInviteRepository(),
        password_resets=FakePasswordResetRepository(),
    )
    await service.ensure_admin("admin@example.com", master_key="secret")
    # Second replica loses the insert race; ensure_admin must swallow the conflict.
    await service.ensure_admin("admin@example.com", master_key="secret")
    assert await users.get_by_email("admin@example.com") is not None
