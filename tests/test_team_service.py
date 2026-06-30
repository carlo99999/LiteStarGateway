"""Unit tests for TeamService authorization, using in-memory fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from litestar_test.application.team_service import TeamService
from litestar_test.domain.entities import (
    Organization,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from litestar_test.domain.exceptions import (
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

    async def list_by_organization(self, organization_id: UUID) -> list[Team]:
        return [t for t in self.items.values() if t.organization_id == organization_id]


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

    async def list_by_team(self, team_id: UUID) -> list[TeamMembership]:
        return [m for m in self.items if m.team_id == team_id]

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
