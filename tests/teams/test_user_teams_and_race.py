"""Round 10: /me/teams completeness (ISSUE-003) and the add_member vs
concurrent-deletion race labeling (ISSUE-006)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.entities import Team, TeamMembership, TeamRole, User
from litestar_gateway.domain.exceptions import AlreadyMember, UserNotFound
from litestar_gateway.domain.ports import (
    APIKeyRepository,
    ModelRepository,
    OrganizationRepository,
    TeamMembershipRepository,
    TeamRepository,
    UserRepository,
)

from .conftest import (
    FakeApiKeyRepo,
    FakeMembershipRepo,
    FakeModelRepo,
    FakeOrgRepo,
    FakeTeamRepo,
    FakeTransaction,
    FakeUserRepo,
)


def _user(email: str = "u@b.com", *, is_admin: bool = False) -> User:
    return User(
        id=uuid4(),
        email=email,
        password_hash="x",  # pragma: allowlist secret
        is_admin=is_admin,
        created_at=datetime.now(UTC),
    )


def _service(
    teams: FakeTeamRepo, memberships: FakeMembershipRepo, users: FakeUserRepo
) -> TeamService:
    # The fakes implement only what these tests exercise; cast to the ports so
    # the type checker accepts the partial test doubles (as the conftest does).
    return TeamService(
        cast(OrganizationRepository, FakeOrgRepo()),
        cast(TeamRepository, teams),
        cast(TeamMembershipRepository, memberships),
        cast(UserRepository, users),
        FakeTransaction(),
        cast(ModelRepository, FakeModelRepo()),
        cast(APIKeyRepository, FakeApiKeyRepo()),
    )


async def test_list_user_teams_returns_all_memberships_across_pages() -> None:
    # ISSUE-003: more than one page of memberships must not be truncated to the
    # first DEFAULT_PAGE_SIZE (100).
    teams, memberships, users = FakeTeamRepo(), FakeMembershipRepo(), FakeUserRepo()
    user = await users.add(_user())
    count = 150
    for i in range(count):
        team = Team(
            id=uuid4(),
            organization_id=uuid4(),
            name=f"team-{i}",
            created_at=datetime.now(UTC),
        )
        await teams.add(team)
        memberships.items.append(
            TeamMembership(
                id=uuid4(),
                team_id=team.id,
                user_id=user.id,
                role=TeamRole.MEMBER,
                created_at=datetime.now(UTC),
            )
        )

    result = await _service(teams, memberships, users).list_user_teams(user)
    assert len(result) == count
    assert {t.name for t, _ in result} == {f"team-{i}" for i in range(count)}


async def test_list_user_teams_skips_memberships_whose_team_vanished() -> None:
    # A membership row can outlive a race with team deletion; list_by_ids simply
    # omits the missing team rather than erroring.
    teams, memberships, users = FakeTeamRepo(), FakeMembershipRepo(), FakeUserRepo()
    user = await users.add(_user())
    memberships.items.append(
        TeamMembership(
            id=uuid4(),
            team_id=uuid4(),  # no matching team in the repo
            user_id=user.id,
            role=TeamRole.MEMBER,
            created_at=datetime.now(UTC),
        )
    )
    assert await _service(teams, memberships, users).list_user_teams(user) == []


class _RaceMembershipRepo(FakeMembershipRepo):
    """add() raises AlreadyMember (what the adapter does for *any* insert
    IntegrityError), simulating the FK violation of a concurrent deletion."""

    async def add(self, m: TeamMembership) -> TeamMembership:
        raise AlreadyMember(str(m.user_id))


async def test_add_member_reports_user_not_found_when_user_deleted_mid_insert() -> None:
    # ISSUE-006: the adapter maps the FK violation to AlreadyMember; the service
    # must re-read state and report the real cause (the user is gone).
    teams, memberships, users = FakeTeamRepo(), _RaceMembershipRepo(), FakeUserRepo()
    admin = await users.add(_user("admin@b.com", is_admin=True))
    team = Team(id=uuid4(), organization_id=uuid4(), name="core", created_at=datetime.now(UTC))
    await teams.add(team)
    # The invitee exists at the pre-check…
    victim = _user("victim@b.com")
    users.by_email[victim.email] = victim

    # …but get(user_id) now returns None (deleted mid-flight): drop it so the
    # post-error re-read sees the user gone.
    async def _gone(_user_id: object) -> None:
        return None

    users.get = _gone  # type: ignore[method-assign]

    with pytest.raises(UserNotFound):
        await _service(teams, memberships, users).add_member(
            admin, team.id, "victim@b.com", TeamRole.MEMBER
        )


async def test_add_member_still_reports_already_member_on_genuine_duplicate() -> None:
    # When the user really still exists, a duplicate insert stays AlreadyMember.
    teams, memberships, users = FakeTeamRepo(), _RaceMembershipRepo(), FakeUserRepo()
    admin = await users.add(_user("admin@b.com", is_admin=True))
    team = Team(id=uuid4(), organization_id=uuid4(), name="core", created_at=datetime.now(UTC))
    await teams.add(team)
    await users.add(_user("dup@b.com"))

    with pytest.raises(AlreadyMember):
        await _service(teams, memberships, users).add_member(
            admin, team.id, "dup@b.com", TeamRole.MEMBER
        )
