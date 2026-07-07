"""TeamService.reconcile_sso_memberships: SSO_TEAM_MAPPING group→team sync."""

from __future__ import annotations

from uuid import uuid4

from litestar_gateway.domain.entities import Team, TeamMembership, TeamRole

from .conftest import _now, _user


async def _governed_team(repos):  # noqa: ANN001, ANN202
    _, teams, _, _ = repos
    return await teams.add(Team(id=uuid4(), organization_id=uuid4(), name="Eng", created_at=_now()))


async def test_sso_reconcile_adds_grant_and_leaves_ungoverned_teams(service, repos) -> None:  # noqa: ANN001, E501
    _, teams, memberships, users = repos
    user = _user("sso@corp.com")
    users.add_user(user)
    governed = await _governed_team(repos)
    other = await teams.add(
        Team(id=uuid4(), organization_id=uuid4(), name="Manual", created_at=_now())
    )
    # A membership added by hand to a team the mapping does not govern.
    await memberships.add(
        TeamMembership(
            id=uuid4(), team_id=other.id, user_id=user.id, role=TeamRole.MEMBER, created_at=_now()
        )
    )

    await service.reconcile_sso_memberships(user.id, {governed.id: TeamRole.ADMIN}, {governed.id})

    assert (await memberships.get(governed.id, user.id)).role is TeamRole.ADMIN
    # The manual membership in the ungoverned team is untouched.
    assert await memberships.get(other.id, user.id) is not None


async def test_sso_reconcile_updates_stale_role(service, repos) -> None:  # noqa: ANN001
    _, _, memberships, users = repos
    user = _user("sso@corp.com")
    users.add_user(user)
    team = await _governed_team(repos)
    await memberships.add(
        TeamMembership(
            id=uuid4(), team_id=team.id, user_id=user.id, role=TeamRole.MEMBER, created_at=_now()
        )
    )
    await service.reconcile_sso_memberships(user.id, {team.id: TeamRole.ADMIN}, {team.id})
    assert (await memberships.get(team.id, user.id)).role is TeamRole.ADMIN


async def test_sso_reconcile_removes_membership_no_longer_granted(service, repos) -> None:  # noqa: ANN001, E501
    _, _, memberships, users = repos
    user = _user("sso@corp.com")
    users.add_user(user)
    team = await _governed_team(repos)
    # A second admin so the reconciled user is not the team's last admin.
    await memberships.add(
        TeamMembership(
            id=uuid4(), team_id=team.id, user_id=uuid4(), role=TeamRole.ADMIN, created_at=_now()
        )
    )
    await memberships.add(
        TeamMembership(
            id=uuid4(), team_id=team.id, user_id=user.id, role=TeamRole.MEMBER, created_at=_now()
        )
    )
    # Team is governed but the user's current groups grant nothing there.
    await service.reconcile_sso_memberships(user.id, {}, {team.id})
    assert await memberships.get(team.id, user.id) is None


async def test_sso_reconcile_skips_deleted_team(service, repos) -> None:  # noqa: ANN001
    _, _, memberships, users = repos
    user = _user("sso@corp.com")
    users.add_user(user)
    ghost = uuid4()  # governed id with no backing team
    await service.reconcile_sso_memberships(user.id, {ghost: TeamRole.MEMBER}, {ghost})
    assert await memberships.get(ghost, user.id) is None


async def test_sso_reconcile_never_strips_last_admin(service, repos) -> None:  # noqa: ANN001
    _, _, memberships, users = repos
    user = _user("sso@corp.com")
    users.add_user(user)
    team = await _governed_team(repos)
    # Sole admin whose group no longer grants the team: removing would orphan it.
    await memberships.add(
        TeamMembership(
            id=uuid4(), team_id=team.id, user_id=user.id, role=TeamRole.ADMIN, created_at=_now()
        )
    )
    await service.reconcile_sso_memberships(user.id, {}, {team.id})
    assert (await memberships.get(team.id, user.id)).role is TeamRole.ADMIN


async def test_sso_reconcile_never_demotes_last_admin(service, repos) -> None:  # noqa: ANN001
    _, _, memberships, users = repos
    user = _user("sso@corp.com")
    users.add_user(user)
    team = await _governed_team(repos)
    await memberships.add(
        TeamMembership(
            id=uuid4(), team_id=team.id, user_id=user.id, role=TeamRole.ADMIN, created_at=_now()
        )
    )
    # Group now grants only MEMBER, but demoting the sole admin is refused.
    await service.reconcile_sso_memberships(user.id, {team.id: TeamRole.MEMBER}, {team.id})
    assert (await memberships.get(team.id, user.id)).role is TeamRole.ADMIN
