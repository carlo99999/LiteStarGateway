"""Unit tests for UserService using in-memory fake repositories."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from litestar_test.application.user_service import UserService
from litestar_test.domain.entities import Invite, PasswordReset, User
from litestar_test.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidInvite,
    InvalidPasswordReset,
    MasterKeyMissing,
    PermissionDenied,
    UserNotFound,
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

    async def get_by_sso_subject(self, subject: str) -> User | None:
        return next((u for u in self._by_email.values() if u.sso_subject == subject), None)

    async def bind_sso(self, user_id: UUID, sso_subject: str, is_admin: bool) -> User:
        for email, user in self._by_email.items():
            if user.id == user_id:
                updated = dataclasses.replace(user, sso_subject=sso_subject, is_admin=is_admin)
                self._by_email[email] = updated
                return updated
        raise AssertionError(f"no user {user_id}")

    async def count(self) -> int:
        return len(self._by_email)

    async def increment_token_version(self, user_id: UUID) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(
                    user, token_version=user.token_version + 1
                )

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(
                    user, password_hash=password_hash, token_version=user.token_version + 1
                )

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
        # Mirror the real repo's conditional UPDATE (consume only if not yet used);
        # expiry is enforced in the service before mark_used is called.
        invite = self._by_id.get(invite_id)
        if invite is None or invite.used_at is not None:
            return False
        self._by_id[invite_id] = dataclasses.replace(invite, used_at=used_at)
        return True


class FakePasswordResetRepository:
    def __init__(self) -> None:
        self._by_id: dict[UUID, PasswordReset] = {}

    async def add(self, reset: PasswordReset) -> PasswordReset:
        self._by_id[reset.id] = reset
        return reset

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None:
        return next((r for r in self._by_id.values() if r.token_hash == token_hash), None)

    async def mark_used(self, reset_id: UUID, used_at: datetime) -> bool:
        reset = self._by_id.get(reset_id)
        if reset is None or reset.used_at is not None:
            return False
        self._by_id[reset_id] = dataclasses.replace(reset, used_at=used_at)
        return True


@pytest.fixture
def service() -> UserService:
    return UserService(
        users=FakeUserRepository(),
        invites=FakeInviteRepository(),
        password_resets=FakePasswordResetRepository(),
    )


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


async def test_register_rejects_expired_invite() -> None:
    invites = FakeInviteRepository()
    svc = UserService(
        users=FakeUserRepository(),
        invites=invites,
        password_resets=FakePasswordResetRepository(),
    )
    issued = await svc.create_invite()
    # Force the stored invite past its expiry.
    invites._by_id[issued.invite.id] = dataclasses.replace(
        invites._by_id[issued.invite.id], expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(InvalidInvite):
        await svc.register(issued.token, "late@b.com", "Passw0rd!")


async def test_create_invite_sets_future_expiry(service: UserService) -> None:
    issued = await service.create_invite()
    assert issued.invite.expires_at > datetime.now(UTC)


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


async def _admin(service: UserService) -> User:
    await service.ensure_admin("admin@example.com", master_key="secret")
    admin = await service._users.get_by_email("admin@example.com")
    assert admin is not None
    return admin


async def _make_user(service: UserService, email: str, password: str) -> None:
    issued = await service.create_invite()
    await service.register(issued.token, email, password)


async def test_create_password_reset_requires_admin(service: UserService) -> None:
    await _make_user(service, "u@b.com", "Passw0rd!")
    non_admin = await service._users.get_by_email("u@b.com")
    assert non_admin is not None
    with pytest.raises(PermissionDenied):
        await service.create_password_reset(non_admin, "u@b.com")


async def test_create_password_reset_unknown_email(service: UserService) -> None:
    admin = await _admin(service)
    with pytest.raises(UserNotFound):
        await service.create_password_reset(admin, "ghost@b.com")


async def test_reset_password_changes_password(service: UserService) -> None:
    admin = await _admin(service)
    await _make_user(service, "u@b.com", "OldPassw0rd!")

    issued = await service.create_password_reset(admin, "u@b.com")
    await service.reset_password(issued.token, "NewPassw0rd!")

    # New password works; old one no longer does.
    await service.authenticate("u@b.com", "NewPassw0rd!")
    with pytest.raises(InvalidCredentials):
        await service.authenticate("u@b.com", "OldPassw0rd!")


async def test_reset_token_is_single_use(service: UserService) -> None:
    admin = await _admin(service)
    await _make_user(service, "u@b.com", "Passw0rd!")
    issued = await service.create_password_reset(admin, "u@b.com")
    await service.reset_password(issued.token, "NewPassw0rd!")
    with pytest.raises(InvalidPasswordReset):
        await service.reset_password(issued.token, "Another1!")


async def test_reset_password_rejects_invalid_token(service: UserService) -> None:
    with pytest.raises(InvalidPasswordReset):
        await service.reset_password("nope", "NewPassw0rd!")


async def test_reset_password_rejects_weak_password(service: UserService) -> None:
    admin = await _admin(service)
    await _make_user(service, "u@b.com", "Passw0rd!")
    issued = await service.create_password_reset(admin, "u@b.com")
    with pytest.raises(WeakPassword):
        await service.reset_password(issued.token, "weak")
