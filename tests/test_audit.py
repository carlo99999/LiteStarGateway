"""Integration tests for the audit trail."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
SALT_KEY = "unit-test-salt-key"
FAKE_OPENAI_VALUE = "sk-x"  # a fake credential value, not a real key
LOGIN_CODE = "Passw0rd!"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def test_privileged_actions_are_audited(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    cred = (
        await client.post(
            "/credentials",
            json={
                "name": "prod-openai",
                "provider": "openai",
                "values": {"api_key": FAKE_OPENAI_VALUE},
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    await client.delete(f"/credentials/{cred}", headers=_bearer(admin))

    events = (await client.get("/audit", headers=_bearer(admin))).json()
    actions = [e["action"] for e in events]
    assert "credential.create" in actions
    assert "credential.delete" in actions
    # Most recent first: the delete was last.
    assert events[0]["action"] == "credential.delete"
    create = next(e for e in events if e["action"] == "credential.create")
    assert create["actor_email"] == ADMIN_EMAIL
    assert create["target_type"] == "credential"
    assert create["target_id"] == cred
    assert create["ip"]  # client address captured
    # The secret value is never in the audit record.
    assert FAKE_OPENAI_VALUE not in (create["detail"] or "")


async def test_audit_read_requires_platform_admin(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "bob@b.com", "password": LOGIN_CODE},
    )
    user = (
        await client.post("/login", json={"email": "bob@b.com", "password": LOGIN_CODE})
    ).json()["access_token"]
    resp = await client.get("/audit", headers=_bearer(user))
    assert resp.status_code == HTTP_403_FORBIDDEN
    # And the admin can read it.
    assert (await client.get("/audit", headers=_bearer(admin))).status_code == HTTP_200_OK
