"""UserService: admin listing and hard-deletion of users."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import TeamMembership, TeamRole
from litestar_gateway.domain.exceptions import (
    PermissionDenied,
    UserHasReferences,
    UserNotFound,
)

from .conftest import (
    FakeAPIKeyRepository,
    FakeInviteRepository,
    FakePasswordResetRepository,
    FakeTeamMembershipRepository,
    FakeTeamRepository,
    FakeTransaction,
    FakeUserRepository,
    _account,
)


def _service(
    users: FakeUserRepository,
    *,
    memberships: FakeTeamMembershipRepository | None = None,
    api_keys: FakeAPIKeyRepository | None = None,
    transaction: FakeTransaction | None = None,
) -> UserService:
    return UserService(
        transaction=transaction or FakeTransaction(),
        users=users,
        invites=FakeInviteRepository(),
        password_resets=FakePasswordResetRepository(),
        teams=FakeTeamRepository(),
        # Minimal fakes: they implement only the lookups the delete guard needs.
        api_keys=api_keys or FakeAPIKeyRepository(),  # type: ignore[bad-argument-type]
        memberships=memberships or FakeTeamMembershipRepository(),  # type: ignore[bad-argument-type]
    )


async def test_list_users_requires_admin() -> None:
    users = FakeUserRepository()
    member = await users.add(_account("m@b.com"))
    with pytest.raises(PermissionDenied):
        await _service(users).list_users(member)


async def test_list_users_returns_all_for_admin() -> None:
    users = FakeUserRepository()
    admin = await users.add(_account("admin@b.com", is_admin=True))
    await users.add(_account("a@b.com"))
    await users.add(_account("b@b.com"))
    listed = await _service(users).list_users(admin)
    assert {u.email for u in listed} == {"admin@b.com", "a@b.com", "b@b.com"}


async def test_delete_user_requires_admin() -> None:
    users = FakeUserRepository()
    member = await users.add(_account("m@b.com"))
    with pytest.raises(PermissionDenied):
        await _service(users).delete_user(member, member.id)


async def test_delete_user_unknown() -> None:
    users = FakeUserRepository()
    admin = await users.add(_account("admin@b.com", is_admin=True))
    with pytest.raises(UserNotFound):
        await _service(users).delete_user(admin, uuid4())


async def test_delete_user_forbids_self_delete() -> None:
    users = FakeUserRepository()
    transaction = FakeTransaction()
    admin = await users.add(_account("admin@b.com", is_admin=True))
    member = await users.add(_account("member@b.com"))

    with pytest.raises(PermissionDenied, match="Cannot delete your own account"):
        await _service(users, transaction=transaction).delete_user(admin, admin.id)

    assert await users.get(admin.id) == admin
    assert await users.get(member.id) == member
    assert transaction.commits == 0
    assert transaction.rollbacks == 0


async def test_delete_user_revalidates_actor_while_admins_are_locked() -> None:
    users = FakeUserRepository()
    stale_admin = await users.add(_account("admin@b.com", is_admin=True))
    victim = await users.add(_account("victim@b.com"))
    await users.set_admin(stale_admin.id, False)

    with pytest.raises(PermissionDenied, match="Platform admin privileges required"):
        await _service(users).delete_user(stale_admin, victim.id)

    assert await users.get(victim.id) == victim


async def test_delete_user_removes_clean_account() -> None:
    users = FakeUserRepository()
    transaction = FakeTransaction()
    admin = await users.add(_account("admin@b.com", is_admin=True))
    victim = await users.add(_account("victim@b.com"))
    deleted = await _service(users, transaction=transaction).delete_user(admin, victim.id)
    assert deleted.email == "victim@b.com"
    assert await users.get(victim.id) is None
    assert transaction.commits == 1
    assert transaction.rollbacks == 0


async def test_delete_user_blocked_by_membership() -> None:
    users = FakeUserRepository()
    admin = await users.add(_account("admin@b.com", is_admin=True))
    victim = await users.add(_account("victim@b.com"))
    memberships = FakeTeamMembershipRepository()
    await memberships.add(
        TeamMembership(
            id=uuid4(),
            team_id=uuid4(),
            user_id=victim.id,
            role=TeamRole.MEMBER,
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(UserHasReferences):
        await _service(users, memberships=memberships).delete_user(admin, victim.id)
    assert await users.get(victim.id) is not None


async def test_delete_user_blocked_by_created_key() -> None:
    users = FakeUserRepository()
    admin = await users.add(_account("admin@b.com", is_admin=True))
    victim = await users.add(_account("victim@b.com"))
    api_keys = FakeAPIKeyRepository()
    api_keys._items.append(SimpleNamespace(created_by=victim.id))
    with pytest.raises(UserHasReferences):
        await _service(users, api_keys=api_keys).delete_user(admin, victim.id)
    assert await users.get(victim.id) is not None
