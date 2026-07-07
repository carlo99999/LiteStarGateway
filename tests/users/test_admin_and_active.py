"""UserService: enable/disable accounts and grant/revoke the platform-admin role."""

from __future__ import annotations

from uuid import uuid4

import pytest

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.exceptions import InvalidCredentials, PermissionDenied, UserNotFound

from .conftest import _account


async def test_set_user_active_disables_and_revokes(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    target = _account("bob@b.com")
    await service._users.add(admin)
    await service._users.add(target)

    updated = await service.set_user_active(admin, target.id, is_active=False)
    assert updated.is_active is False
    assert updated.token_version == target.token_version + 1  # sessions revoked


async def test_set_user_active_requires_platform_admin(service: UserService) -> None:
    non_admin = _account("nope@b.com")
    other = _account("other@b.com")
    await service._users.add(non_admin)
    await service._users.add(other)
    with pytest.raises(PermissionDenied):
        await service.set_user_active(non_admin, other.id, is_active=False)


async def test_set_user_active_forbids_self_disable(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    await service._users.add(admin)
    with pytest.raises(PermissionDenied):
        await service.set_user_active(admin, admin.id, is_active=False)


async def test_disabled_user_cannot_authenticate(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    target = _account("bob@b.com")
    await service._users.add(admin)
    await service._users.add(target)
    await service.set_user_active(admin, target.id, is_active=False)
    with pytest.raises(InvalidCredentials):
        await service.authenticate("bob@b.com", "Passw0rd!")


async def test_set_user_admin_promotes_and_demotes(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    target = _account("bob@b.com")
    await service._users.add(admin)
    await service._users.add(target)

    promoted = await service.set_user_admin(admin, target.id, is_admin=True)
    assert promoted.is_admin is True
    demoted = await service.set_user_admin(admin, target.id, is_admin=False)
    assert demoted.is_admin is False


async def test_set_user_admin_requires_platform_admin(service: UserService) -> None:
    non_admin = _account("nope@b.com")
    other = _account("other@b.com")
    await service._users.add(non_admin)
    await service._users.add(other)
    with pytest.raises(PermissionDenied):
        await service.set_user_admin(non_admin, other.id, is_admin=True)


async def test_set_user_admin_forbids_changing_own_role(service: UserService) -> None:
    # An admin demoting themselves is a self-lockout footgun (mirrors set_active).
    admin = _account("admin@b.com", is_admin=True)
    await service._users.add(admin)
    with pytest.raises(PermissionDenied):
        await service.set_user_admin(admin, admin.id, is_admin=False)


async def test_set_user_admin_unknown_user(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    await service._users.add(admin)
    with pytest.raises(UserNotFound):
        await service.set_user_admin(admin, uuid4(), is_admin=True)
