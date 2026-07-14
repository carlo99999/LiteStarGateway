"""Shared fixtures for tests/teams/: TeamService unit-test fakes (in-memory
repositories) plus the integration `client` + helpers for the API-level tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from litestar.status_codes import HTTP_201_CREATED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Organization, Team, TeamMembership, User
from litestar_gateway.domain.ports import APIKeyRepository, ModelRepository

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
DEV_PASSWORD = "S3cure-pass-123"  # pragma: allowlist secret


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

    async def lock_for_lifecycle(self, team_id: UUID) -> Team | None:
        return self.items.get(team_id)

    async def list_by_organization(
        self, organization_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[Team]:
        rows = [t for t in self.items.values() if t.organization_id == organization_id]
        return rows[offset : offset + limit]

    async def delete(self, team_id: UUID) -> None:
        self.items.pop(team_id, None)


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

    async def set_active(
        self, user_id: UUID, is_active: bool, *, deactivated_by: str | None = None
    ) -> None:  # pragma: no cover
        return None


class FakeModelRepo:
    """Only `list_by_team` is exercised (the team-delete guard)."""

    async def list_by_team(self, team_id: UUID, *, limit: int = 100, offset: int = 0) -> list:
        return []


class FakeApiKeyRepo:
    async def list_by_team(self, team_id: UUID, *, limit: int = 100, offset: int = 0) -> list:
        return []


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
    # The fakes intentionally implement only what these unit tests exercise; cast
    # to the ports so the type checker accepts the partial test doubles.
    return TeamService(
        orgs,
        teams,
        memberships,
        users,
        FakeTransaction(),
        cast(ModelRepository, FakeModelRepo()),
        cast(APIKeyRepository, FakeApiKeyRepo()),
    )


# ── Integration fixtures (API-level tests: service principals, keys, teams) ──


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sp.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


async def _admin(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _team_and_credential(
    client: AsyncTestClient, admin: str, name: str = "Core"
) -> tuple[str, str]:
    cred = (
        await client.post(
            "/credentials",
            json={
                "name": f"c-{name}",
                "provider": "openai",
                "values": {"api_key": "sk-x"},  # pragma: allowlist secret
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": f"O-{name}"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": name, "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    return team, cred


async def _sp(client: AsyncTestClient, admin: str, team: str, name: str = "ci") -> str:
    resp = await client.post(
        f"/teams/{team}/service-principals", json={"name": name}, headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _sp_key(
    client: AsyncTestClient, admin: str, team: str, sp: str, scope: str = "management"
) -> str:
    resp = await client.post(
        f"/teams/{team}/service-principals/{sp}/keys",
        json={"name": "k", "scope": scope},
        headers=_bearer(admin),
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["plaintext"]


def _model(cred: str, name: str = "m") -> dict:
    return {
        "name": name,
        "provider": "openai",
        "credential_id": cred,
        "type": "chat",
        "provider_model_id": "gpt-4o",
        "enabled": True,
    }
