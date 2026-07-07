"""UserService: signup via invite, and admin-issued password resets."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    FakeInviteRepository,
    FakePasswordResetRepository,
    FakeTransaction,
    FakeUserRepository,
)

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidInvite,
    InvalidPasswordReset,
    PermissionDenied,
    UserNotFound,
    WeakPassword,
)


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
        transaction=FakeTransaction(),
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


async def test_async_password_wrappers_roundtrip() -> None:
    """The a* variants offload Argon2 to a thread but must match the sync hasher."""
    from litestar_gateway.domain.password import ahash_password, averify_password, verify_password

    hashed = await ahash_password("Passw0rd!")
    assert verify_password("Passw0rd!", hashed)
    assert await averify_password("Passw0rd!", hashed)
    assert not await averify_password("wrong-password", hashed)
