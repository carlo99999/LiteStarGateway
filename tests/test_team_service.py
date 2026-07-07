"""Unit tests for TeamService authorization, using in-memory fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.entities import (
    Organization,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from litestar_gateway.domain.exceptions import (
    LastTeamAdmin,
    OrganizationNotFound,
    PermissionDenied,
    TeamNotFound,
    UserNotFound,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _user(email: str, *, is_admin: bool = False) -> User:
    return User(id=uuid4(), email=email, password_hash="x", is_admin=is_admin, created_at=_now())


class FakeOrgRepo:
    def __init__(self) -> None:
        self.items: dict[UUID, Organization] = {}

    async def add(self, org: Organization) -> Organization:
        self.items[org.id] = org
        return org

    async def get(self, organization_id: UUID) -> Organization | None:
        return self.items.get(organization_id)

    async def list(self) -> list[Organization]:
        return list(self.items.values())


class FakeTeamRepo:
    def __init__(self) -> None:
        self.items: dict[UUID, Team] = {}

    async def add(self, team: Team) -> Team:
        self.items[team.id] = team
        return team

    async def get(self, team_id: UUID) -> Team | None:
        return self.items.get(team_id)

    async def list_by_organization(
        self, organization_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[Team]:
        rows = [t for t in self.items.values() if t.organization_id == organization_id]
        return rows[offset : offset + limit]


class FakeMembershipRepo:
    def __init__(self) -> None:
        self.items: list[TeamMembership] = []

    async def add(self, m: TeamMembership) -> TeamMembership:
        self.items.append(m)
        return m

    async def get(self, team_id: UUID, user_id: UUID) -> TeamMembership | None:
        return next(
            (m for m in self.items if m.team_id == team_id and m.user_id == user_id),
            None,
        )

    async def list_by_team(
        self, team_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[TeamMembership]:
        # Honor limit/offset like the real repo, so tests can exercise the
        # >1-page truncation that M34 was about.
        rows = [m for m in self.items if m.team_id == team_id]
        return rows[offset : offset + limit]

    async def count_admins(self, team_id: UUID) -> int:
        return sum(1 for m in self.items if m.team_id == team_id and m.is_admin)

    async def update(self, m: TeamMembership) -> TeamMembership:
        self.items = [x for x in self.items if x.id != m.id] + [m]
        return m

    async def remove(self, team_id: UUID, user_id: UUID) -> None:
        self.items = [m for m in self.items if not (m.team_id == team_id and m.user_id == user_id)]


class FakeUserRepo:
    def __init__(self) -> None:
        self.by_email: dict[str, User] = {}

    def add_user(self, user: User) -> None:
        self.by_email[user.email] = user

    async def add(self, user: User) -> User:
        self.by_email[user.email] = user
        return user

    async def get(self, user_id: UUID) -> User | None:
        return next((u for u in self.by_email.values() if u.id == user_id), None)

    async def get_by_email(self, email: str) -> User | None:
        return self.by_email.get(email)

    async def count(self) -> int:
        return len(self.by_email)

    async def increment_token_version(self, user_id: UUID) -> None:  # pragma: no cover
        return None

    async def set_active(self, user_id: UUID, is_active: bool) -> None:  # pragma: no cover
        return None


class FakeTransaction:
    """No-op unit-of-work boundary; the in-memory fakes persist on write."""

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:  # pragma: no cover - no failing path exercised
        return None


@pytest.fixture
def repos() -> tuple[FakeOrgRepo, FakeTeamRepo, FakeMembershipRepo, FakeUserRepo]:
    return FakeOrgRepo(), FakeTeamRepo(), FakeMembershipRepo(), FakeUserRepo()


@pytest.fixture
def service(repos) -> TeamService:  # noqa: ANN001
    orgs, teams, memberships, users = repos
    return TeamService(orgs, teams, memberships, users, FakeTransaction())


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


async def test_ensure_can_manage_unknown_team(service, repos) -> None:  # noqa: ANN001
    _, _, _, users = repos
    admin = _user("admin@b.com", is_admin=True)
    users.add_user(admin)
    with pytest.raises(TeamNotFound):
        await service.ensure_can_manage_team(admin, uuid4())


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
