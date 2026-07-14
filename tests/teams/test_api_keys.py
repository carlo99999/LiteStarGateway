"""Integration tests for team-scoped API keys + API-key authentication."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'keys.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncTestClient, email: str, password: str) -> str:
    resp = await client.post("/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def _setup_team(client: AsyncTestClient) -> tuple[str, str]:
    """Bootstrap admin → org → team (admin is team admin). Returns (token, team_id)."""
    token = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(token))
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(token),
        )
    ).json()["id"]
    return token, team_id


async def _create_key(client: AsyncTestClient, token: str, team_id: str) -> dict:
    resp = await client.post(f"/teams/{team_id}/keys", json={"name": "ci"}, headers=_bearer(token))
    assert resp.status_code == HTTP_201_CREATED
    return resp.json()


async def test_create_key_returns_plaintext_once(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    body = await _create_key(client, token, team_id)
    assert body["team_id"] == team_id
    assert body["plaintext"].startswith("lsk_")
    assert body["prefix"] == body["plaintext"][: len(body["prefix"])]


async def test_protected_route_rejects_without_key(client: AsyncTestClient) -> None:
    assert (await client.get("/whoami")).status_code == HTTP_401_UNAUTHORIZED


async def test_protected_route_rejects_invalid_key(client: AsyncTestClient) -> None:
    resp = await client.get("/whoami", headers=_bearer("lsk_nope"))
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_valid_key_grants_access_and_reports_team(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    key = (await _create_key(client, token, team_id))["plaintext"]
    resp = await client.get("/whoami", headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    assert resp.json() == {"team_id": team_id}


async def test_revoked_key_is_rejected(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    created = await _create_key(client, token, team_id)
    key, key_id = created["plaintext"], created["id"]

    assert (await client.get("/whoami", headers=_bearer(key))).status_code == HTTP_200_OK

    revoke = await client.delete(f"/teams/{team_id}/keys/{key_id}", headers=_bearer(token))
    assert revoke.status_code in (200, 204)

    after = await client.get("/whoami", headers=_bearer(key))
    assert after.status_code == HTTP_401_UNAUTHORIZED


async def test_list_keys_hides_secret(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    await _create_key(client, token, team_id)
    resp = await client.get(f"/teams/{team_id}/keys", headers=_bearer(token))
    assert resp.status_code == HTTP_200_OK
    rows = resp.json()
    assert len(rows) == 1
    assert "plaintext" not in rows[0]
    assert "key_hash" not in rows[0]
    assert rows[0]["is_active"] is True


async def test_key_creation_requires_team_admin(client: AsyncTestClient) -> None:
    admin_token, team_id = await _setup_team(client)
    invite = await seed_team_and_invite(client, admin_token)
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "outsider@b.com", "password": "Passw0rd!"},
    )
    outsider = await _login(client, "outsider@b.com", "Passw0rd!")
    resp = await client.post(
        f"/teams/{team_id}/keys", json={"name": "x"}, headers=_bearer(outsider)
    )
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_personal_key_carries_rate_limit(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    resp = await client.post(
        f"/teams/{team_id}/keys",
        json={"name": "k", "rate_limit_rpm": 30},
        headers=_bearer(token),
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["rate_limit_rpm"] == 30
    listed = (await client.get(f"/teams/{team_id}/keys", headers=_bearer(token))).json()
    assert any(k["rate_limit_rpm"] == 30 for k in listed)


async def test_service_principal_key_carries_rate_limit(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    sp = await client.post(
        f"/teams/{team_id}/service-principals",
        json={"name": "bot"},
        headers=_bearer(token),
    )
    assert sp.status_code in (200, 201), sp.text
    sp_id = sp.json()["id"]
    resp = await client.post(
        f"/teams/{team_id}/service-principals/{sp_id}/keys",
        json={"name": "botkey", "scope": "management", "rate_limit_rpm": 50},
        headers=_bearer(token),
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["rate_limit_rpm"] == 50


async def test_rotate_issues_new_key_and_keeps_old_during_grace(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    created = await _create_key(client, token, team_id)
    old_plain, key_id = created["plaintext"], created["id"]
    assert (await client.get("/whoami", headers=_bearer(old_plain))).status_code == 200

    rot = await client.post(f"/teams/{team_id}/keys/{key_id}/rotate", headers=_bearer(token))
    assert rot.status_code in (200, 201), rot.text
    new_plain = rot.json()["plaintext"]
    assert new_plain != old_plain
    assert rot.json()["scope"] == created["scope"]

    # During the grace window both the old and the new key work.
    assert (await client.get("/whoami", headers=_bearer(old_plain))).status_code == 200
    assert (await client.get("/whoami", headers=_bearer(new_plain))).status_code == 200
    audit = (await client.get("/audit", headers=_bearer(token))).json()
    rotation = next(event for event in audit if event["action"] == "api_key.rotate")
    assert new_plain not in (rotation["detail"] or "")
    assert rot.json()["id"] in (rotation["detail"] or "")


async def test_key_cannot_be_rotated_twice_during_grace(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    created = await _create_key(client, token, team_id)

    first = await client.post(
        f"/teams/{team_id}/keys/{created['id']}/rotate", headers=_bearer(token)
    )
    assert first.status_code == HTTP_201_CREATED, first.text

    second = await client.post(
        f"/teams/{team_id}/keys/{created['id']}/rotate", headers=_bearer(token)
    )
    assert second.status_code == 404, second.text


@pytest.mark.parametrize("failure_point", ["grace", "audit"])
async def test_rotate_failure_rolls_back_replacement_and_grace(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    token, team_id = await _setup_team(client)
    created = await _create_key(client, token, team_id)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected rotation failure")

    if failure_point == "grace":
        from litestar_gateway.infrastructure.persistence import repository

        monkeypatch.setattr(repository.SQLAlchemyAPIKeyRepository, "schedule_revocation", boom)
    else:
        from litestar_gateway.infrastructure.persistence import audit_repository

        monkeypatch.setattr(audit_repository.SQLAlchemyAuditLog, "stage", boom)

    failed = await client.post(
        f"/teams/{team_id}/keys/{created['id']}/rotate", headers=_bearer(token)
    )
    assert failed.status_code == 500
    monkeypatch.undo()

    keys = (await client.get(f"/teams/{team_id}/keys", headers=_bearer(token))).json()
    assert [key["id"] for key in keys] == [created["id"]]
    assert keys[0]["is_active"] is True
    assert (
        await client.get("/whoami", headers=_bearer(created["plaintext"]))
    ).status_code == HTTP_200_OK


async def test_rotate_revoked_key_is_404(client: AsyncTestClient) -> None:
    token, team_id = await _setup_team(client)
    created = await _create_key(client, token, team_id)
    key_id = created["id"]
    await client.delete(f"/teams/{team_id}/keys/{key_id}", headers=_bearer(token))
    rot = await client.post(f"/teams/{team_id}/keys/{key_id}/rotate", headers=_bearer(token))
    assert rot.status_code == 404, rot.text


def test_apikey_is_active_honours_grace_window() -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from litestar_gateway.domain.entities import APIKey

    base = {
        "id": uuid4(),
        "team_id": uuid4(),
        "created_by": uuid4(),
        "name": None,
        "prefix": "x",
        "key_hash": "h",
        "created_at": datetime.now(UTC),
        "last_used_at": None,
    }
    assert APIKey(**base, revoked_at=None).is_active
    assert APIKey(**base, revoked_at=datetime.now(UTC) + timedelta(hours=1)).is_active
    assert not APIKey(**base, revoked_at=datetime.now(UTC) - timedelta(seconds=1)).is_active
