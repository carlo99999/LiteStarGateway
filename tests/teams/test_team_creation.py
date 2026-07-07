"""TeamService.create_team: platform-admin only, dual-admin bootstrap."""

from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import _now, _user

from litestar_gateway.domain.entities import Organization, TeamRole
from litestar_gateway.domain.exceptions import OrganizationNotFound, PermissionDenied, UserNotFound


async def test_create_team_requires_platform_admin(service, repos) -> None:  # noqa: ANN001
    _, _, _, users = repos
    non_admin = _user("nope@b.com")
    users.add_user(non_admin)
    with pytest.raises(PermissionDenied):
        await service.create_team(non_admin, uuid4(), "T", "nope@b.com")


async def test_create_team_unknown_org(service, repos) -> None:  # noqa: ANN001
    _, _, _, users = repos
    admin = _user("admin@b.com", is_admin=True)
    users.add_user(admin)
    with pytest.raises(OrganizationNotFound):
        await service.create_team(admin, uuid4(), "T", "admin@b.com")


async def test_create_team_unknown_admin_email(service, repos) -> None:  # noqa: ANN001
    orgs, _, _, users = repos
    admin = _user("admin@b.com", is_admin=True)
    users.add_user(admin)
    org = await orgs.add(Organization(id=uuid4(), name="O", created_at=_now()))
    with pytest.raises(UserNotFound):
        await service.create_team(admin, org.id, "T", "ghost@b.com")


async def test_create_team_makes_platform_admin_and_lead_admins(service, repos) -> None:  # noqa: ANN001, E501
    orgs, _, memberships, users = repos
    admin = _user("admin@b.com", is_admin=True)
    lead = _user("lead@b.com")
    for u in (admin, lead):
        users.add_user(u)
    org = await orgs.add(Organization(id=uuid4(), name="O", created_at=_now()))

    team = await service.create_team(admin, org.id, "T", "lead@b.com")

    members = await memberships.list_by_team(team.id)
    assert len(members) == 2
    assert {m.user_id for m in members} == {admin.id, lead.id}
    assert all(m.role is TeamRole.ADMIN for m in members)


async def test_create_team_dedups_when_lead_is_platform_admin(service, repos) -> None:  # noqa: ANN001, E501
    orgs, _, memberships, users = repos
    admin = _user("admin@b.com", is_admin=True)
    users.add_user(admin)
    org = await orgs.add(Organization(id=uuid4(), name="O", created_at=_now()))

    team = await service.create_team(admin, org.id, "T", "admin@b.com")

    members = await memberships.list_by_team(team.id)
    assert len(members) == 1
    assert members[0].user_id == admin.id
    assert members[0].role is TeamRole.ADMIN


async def test_team_admin_can_manage_but_member_cannot(service, repos) -> None:  # noqa: ANN001
    orgs, _, _, users = repos
    admin = _user("admin@b.com", is_admin=True)
    lead = _user("lead@b.com")
    member = _user("member@b.com")
    for u in (admin, lead, member):
        users.add_user(u)
    org = await orgs.add(Organization(id=uuid4(), name="O", created_at=_now()))
    team = await service.create_team(admin, org.id, "T", "lead@b.com")

    # lead (team admin) can add a member
    await service.add_member(lead, team.id, "member@b.com", TeamRole.MEMBER)
    # member cannot
    with pytest.raises(PermissionDenied):
        await service.add_member(member, team.id, "admin@b.com", TeamRole.MEMBER)
