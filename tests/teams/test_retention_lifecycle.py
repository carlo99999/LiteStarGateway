"""Plan 13 Phase 5 — retention and deletion lifecycle.

Covers: soft-delete (tombstone) for a team with billed history vs. the
unchanged hard-delete fast path for a team without one, the export-before-delete
action, and the separate, audited, admin-only purge action.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from _invite_helpers import issue_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.infrastructure.persistence.orm import TeamModel, UsageEventModel

from .conftest import ADMIN_EMAIL, MASTER_KEY, _bearer, _team_and_credential


async def _add_member(client: AsyncTestClient, admin_token: str, team_id: str, email: str) -> str:
    """Sign up a plain (non-admin) member of `team_id` and log them in."""
    invite = await issue_invite(client, admin_token, team_id, role="member")
    signup = await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": email,
            "password": "Passw0rd!",  # pragma: allowlist secret
        },
    )
    assert signup.status_code == HTTP_201_CREATED, signup.text
    return await _login(client, email, "Passw0rd!")  # pragma: allowlist secret


async def _login(client: AsyncTestClient, email: str, password: str) -> str:
    resp = await client.post("/login", json={"email": email, "password": password})
    assert resp.status_code == HTTP_200_OK, resp.text
    return resp.json()["access_token"]


@pytest.fixture
async def raw_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """A second, independent connection to the SAME database as `client` —
    for seeding usage events and inspecting rows the HTTP API deliberately
    hides (a soft-deleted team's row, its retained usage history)."""
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_usage_event(session: AsyncSession, team_id: str) -> None:
    session.add(
        UsageEventModel(
            id=uuid4(),
            team_id=UUID(team_id),
            model_id=uuid4(),
            model_name="gpt-4o",
            operation="chat",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.01,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def test_delete_team_without_usage_is_still_hard_deleted(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """Regression: a team with NO billed history is removed exactly as
    before — no tombstone row left behind."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "NoUsage")

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    model = await raw_session.get(TeamModel, UUID(team_id))
    assert model is None


async def test_delete_team_with_usage_history_is_soft_deleted(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post("/organizations", json={"name": "O-tombstone"}, headers=_bearer(admin))
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Billed", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _seed_usage_event(raw_session, team_id)

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    # Hidden from the ordinary read/list paths...
    assert (
        await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_404_NOT_FOUND
    listed = (await client.get("/teams", headers=_bearer(admin))).json()
    assert team_id not in {t["id"] for t in listed}

    # ...but nothing was actually lost: the team row and its usage history are
    # both still there, tombstoned rather than gone.
    model = await raw_session.get(TeamModel, UUID(team_id))
    assert model is not None
    assert model.deleted_at is not None
    usage_rows = (
        await raw_session.scalars(
            select(UsageEventModel).where(UsageEventModel.team_id == UUID(team_id))
        )
    ).all()
    assert len(usage_rows) == 1


async def test_export_team_returns_usage_and_audit_history(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "Export")
    await _seed_usage_event(raw_session, team_id)
    # Generates a "team.update" audit event targeting this team.
    renamed = await client.patch(
        f"/teams/{team_id}", json={"name": "Exported"}, headers=_bearer(admin)
    )
    assert renamed.status_code == HTTP_200_OK, renamed.text

    resp = await client.get(f"/teams/{team_id}/export", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["team"]["id"] == team_id
    assert len(body["usage_events"]) == 1
    assert body["usage_events"][0]["cost"] == pytest.approx(0.01)
    assert any(e["action"] == "team.update" for e in body["audit_events"])
    assert body["routing_savings"] == {
        "total_estimated_savings": 0.0,
        "decisions_counted": 0,
        "decisions_without_usage": 0,
    }


async def test_export_team_forbidden_for_non_admin(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "ExportRBAC")
    member = await _add_member(client, admin, team_id, "member@example.com")

    resp = await client.get(f"/teams/{team_id}/export", headers=_bearer(member))
    assert resp.status_code == HTTP_403_FORBIDDEN, resp.text


async def test_purge_requires_platform_admin(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "PurgeRBAC")
    not_admin = await _add_member(client, admin, team_id, "notadmin@example.com")
    await _seed_usage_event(raw_session, team_id)
    assert (
        await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_204_NO_CONTENT

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(not_admin))
    assert resp.status_code == HTTP_403_FORBIDDEN, resp.text


async def test_purge_requires_soft_deleted_first(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "PurgeLive")

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert resp.status_code == HTTP_409_CONFLICT, resp.text


async def test_purge_removes_data_and_is_audited(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post("/organizations", json={"name": "O-purge"}, headers=_bearer(admin))
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "ToPurge", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _seed_usage_event(raw_session, team_id)
    assert (
        await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_204_NO_CONTENT

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    # Genuinely gone: a direct repository query, not just the hidden API view.
    assert await raw_session.get(TeamModel, UUID(team_id)) is None
    usage_rows = (
        await raw_session.scalars(
            select(UsageEventModel).where(UsageEventModel.team_id == UUID(team_id))
        )
    ).all()
    assert usage_rows == []

    # And it left an audit trail of its own.
    audit = (await client.get("/audit", headers=_bearer(admin))).json()
    purge_events = [e for e in audit if e["action"] == "team.purge" and e["target_id"] == team_id]
    assert len(purge_events) == 1
    assert purge_events[0]["actor_email"] == ADMIN_EMAIL


async def test_purge_twice_is_not_found_second_time(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """Purge is genuinely irreversible: once the team row is gone, a second
    purge attempt has nothing left to act on (404), never a silent success."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "PurgeTwice")
    await _seed_usage_event(raw_session, team_id)
    await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    first = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert first.status_code == HTTP_204_NO_CONTENT, first.text

    second = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert second.status_code == HTTP_404_NOT_FOUND, second.text
