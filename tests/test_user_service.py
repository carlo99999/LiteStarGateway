"""Unit tests for UserService using in-memory fake repositories."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from uuid import UUID

import pytest

from litestar_test.application.user_service import UserService
from litestar_test.domain.entities import Invite, User
from litestar_test.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidInvite,
    MasterKeyMissing,
    WeakPassword,
)


class FakeUserRepository:
    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}

    async def add(self, user: User) -> User:
        self._by_email[user.email] = user
        return user

    async def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(email)

    async def count(self) -> int:
        return len(self._by_email)

    async def get(self, user_id: UUID) -> User | None:
        return next((u for u in self._by_email.values() if u.id == user_id), None)


class FakeInviteRepository:
    def __init__(self) -> None:
        self._by_id: dict[UUID, Invite] = {}

    async def add(self, invite: Invite) -> Invite:
        self._by_id[invite.id] = invite
        return invite

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        return next((i for i in self._by_id.values() if i.token_hash == token_hash), None)

    async def mark_used(self, invite_id: UUID, used_at: datetime) -> bool:
        invite = self._by_id.get(invite_id)
        if invite is None or not invite.is_usable:
            return False
        self._by_id[invite_id] = dataclasses.replace(invite, used_at=used_at)
        return True


@pytest.fixture
def service() -> UserService:
    return UserService(users=FakeUserRepository(), invites=FakeInviteRepository())


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


async def test_register_requires_valid_invite(service: UserService) -> None:
    with pytest.raises(InvalidInvite):
        await service.register("bad-token", "a@b.com", "PASSw0rd!")


async def test_register_consumes_invite(service: UserService) -> None:
    issued = await service.create_invite()
    await service.register(issued.token, "a@b.com", "Passw0rd!")
    with pytest.raises(InvalidInvite):
        await service.register(issued.token, "c@d.com", "Passw0rd!")


async def test_register_rejects_duplicate_email(service: UserService) -> None:
    i1 = await service.create_invite()
    i2 = await service.create_invite()
    await service.register(i1.token, "dup@b.com", "Passw0rd!")
    with pytest.raises(EmailAlreadyRegistered):
        await service.register(i2.token, "dup@b.com", "Passw0rd!")


@pytest.mark.parametrize(
    "password",
    [
        "Short1!",  # too short (7 chars)
        "alllowercase1!",  # no uppercase
        "ALLUPPERCASE1!",  # no lowercase
        "NoDigitsHere!",  # no digit
        "NoSpecial1A",  # no special character
    ],
)
async def test_register_rejects_weak_password(service: UserService, password: str) -> None:
    issued = await service.create_invite()
    with pytest.raises(WeakPassword):
        await service.register(issued.token, "weak@b.com", password)
