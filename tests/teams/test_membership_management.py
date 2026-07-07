"""TeamService: the last-admin guard on remove/demote, across membership pages."""

from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import _now, _user

from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import Organization, Team, TeamMembership, TeamRole
from litestar_gateway.domain.exceptions import LastTeamAdmin, TeamNotFound


async def test_ensure_can_manage_unknown_team(service, repos) -> None:  # noqa: ANN001
    _, _, _, users = repos
    admin = _user("admin@b.com", is_admin=True)
    users.add_user(admin)
    with pytest.raises(TeamNotFound):
        await service.ensure_team_permission(admin, uuid4(), Permission.MEMBERS_MANAGE)


async def test_last_admin_protected_when_admin_is_beyond_first_page(service, repos) -> None:  # noqa: ANN001, E501
    # M34: the sole admin sits past the first page of memberships (e.g. added
    # after 100 members). _is_last_admin must count admins, not scan one page —
    # otherwise the admin is invisible and demote/remove wrongly succeeds.
    _, teams, memberships, users = repos
    actor = _user("platform@b.com", is_admin=True)
    users.add_user(actor)
    team = await teams.add(Team(id=uuid4(), organization_id=uuid4(), name="Big", created_at=_now()))
    for _ in range(100):  # 100 members occupy the whole first page
        await memberships.add(
            TeamMembership(
                id=uuid4(),
                team_id=team.id,
                user_id=uuid4(),
                role=TeamRole.MEMBER,
                created_at=_now(),
            )
        )
    admin_user = _user("teamadmin@b.com")  # sole admin, added last -> page 2
    await memberships.add(
        TeamMembership(
            id=uuid4(),
            team_id=team.id,
            user_id=admin_user.id,
            role=TeamRole.ADMIN,
            created_at=_now(),
        )
    )

    with pytest.raises(LastTeamAdmin):
        await service.remove_member(actor, team.id, admin_user.id)
    with pytest.raises(LastTeamAdmin):
        await service.set_role(actor, team.id, admin_user.id, TeamRole.MEMBER)


async def _team_with_two_admins(service, repos):  # noqa: ANN001, ANN202
    orgs, _, _, users = repos
    admin = _user("admin@b.com", is_admin=True)
    lead = _user("lead@b.com")
    for u in (admin, lead):
        users.add_user(u)
    org = await orgs.add(Organization(id=uuid4(), name="O", created_at=_now()))
    team = await service.create_team(admin, org.id, "T", "lead@b.com")
    return admin, lead, team


async def test_cannot_remove_last_admin(service, repos) -> None:  # noqa: ANN001
    admin, lead, team = await _team_with_two_admins(service, repos)
    # Two admins → removing one is allowed.
    await service.remove_member(admin, team.id, lead.id)
    # Now `admin` is the only admin → removing them is blocked.
    with pytest.raises(LastTeamAdmin):
        await service.remove_member(admin, team.id, admin.id)


async def test_cannot_demote_last_admin(service, repos) -> None:  # noqa: ANN001
    admin, lead, team = await _team_with_two_admins(service, repos)
    # Demoting one of two admins is fine.
    await service.set_role(admin, team.id, lead.id, TeamRole.MEMBER)
    # `admin` is now the sole admin → demoting them is blocked.
    with pytest.raises(LastTeamAdmin):
        await service.set_role(admin, team.id, admin.id, TeamRole.MEMBER)


async def test_removing_a_member_never_triggers_last_admin_guard(service, repos) -> None:  # noqa: ANN001, E501
    admin, lead, team = await _team_with_two_admins(service, repos)
    _, _, _, users = repos
    member = _user("member@b.com")
    users.add_user(member)
    await service.add_member(admin, team.id, "member@b.com", TeamRole.MEMBER)
    # Removing a plain member is always allowed, regardless of admin count.
    await service.remove_member(admin, team.id, member.id)
