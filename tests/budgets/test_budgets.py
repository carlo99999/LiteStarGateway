"""Integration tests for team budgets: admin CRUD + end-to-end enforcement.

Budgets are hard spend caps per team: set/removed by a platform admin,
readable by team admins (with current-window spend), and enforced pre-call
on the inference endpoints (402 once the window's spend reaches the limit).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
SALT_KEY = "unit-test-salt-key"
OPENAI_VALUES = {"api_key": "sk-x"}  # pragma: allowlist secret
DEV_PASSWORD = "S3cure-pass-123"  # pragma: allowlist secret


class _Result:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


class FakeClient:
    """Minimal OpenAI chat fake: 1 prompt + 1 completion token per call."""

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=self)

    async def close(self) -> None:
        return None

    async def create(self, **kwargs):
        return _Result(
            {
                "id": "cmpl-x",
                "object": "chat.completion",
                "created": 123,
                "model": kwargs.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


@pytest.fixture
async def client(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", FakeClient)
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _setup_team(client: AsyncTestClient) -> tuple[str, str, str]:
    """Credential + org + team + costed model 'm' (0.01 USD/token both ways) +
    key. Each call to the fake model costs 0.02 USD (1+1 tokens).

    Returns (team API key, team id, admin token)."""
    admin = await _admin(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": "c", "provider": "openai", "values": OPENAI_VALUES},
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
            "enabled": True,
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.01,
        },
        headers=_bearer(admin),
    )
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def _chat(client: AsyncTestClient, key: str):
    return await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )


async def test_budget_crud(client: AsyncTestClient) -> None:
    _, team, admin = await _setup_team(client)

    # No budget yet.
    resp = await client.get(f"/teams/{team}/budget", headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND

    # Set (platform admin).
    resp = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 0.03, "window": "monthly"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["limit_cost"] == 0.03
    assert body["window"] == "monthly"
    assert body["spent"] == 0.0
    assert body["remaining"] == 0.03

    # Replace (upsert semantics).
    resp = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 5.0, "window": "daily"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK
    assert resp.json()["limit_cost"] == 5.0
    assert resp.json()["window"] == "daily"

    # Remove.
    resp = await client.delete(f"/teams/{team}/budget", headers=_bearer(admin))
    assert resp.status_code in (200, 204)
    resp = await client.get(f"/teams/{team}/budget", headers=_bearer(admin))
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_budget_validation(client: AsyncTestClient) -> None:
    _, team, admin = await _setup_team(client)

    for bad in (
        {"limit_cost": 0, "window": "monthly"},
        {"limit_cost": -1.0, "window": "monthly"},
        {"limit_cost": 1.0, "window": "yearly"},
    ):
        resp = await client.put(f"/teams/{team}/budget", json=bad, headers=_bearer(admin))
        assert resp.status_code == HTTP_400_BAD_REQUEST, bad


async def test_budget_enforced_end_to_end(client: AsyncTestClient) -> None:
    key, team, admin = await _setup_team(client)

    resp = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 0.03, "window": "monthly"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK

    # Each call costs 0.02: spend 0.00 → ok, 0.02 → ok, 0.04 ≥ 0.03 → blocked.
    assert (await _chat(client, key)).status_code == HTTP_200_OK
    assert (await _chat(client, key)).status_code == HTTP_200_OK
    blocked = await _chat(client, key)
    assert blocked.status_code == HTTP_402_PAYMENT_REQUIRED
    assert "budget" in blocked.json()["error"]["message"].lower()

    # The budget view reflects the spend; remaining never goes negative.
    resp = await client.get(f"/teams/{team}/budget", headers=_bearer(admin))
    assert resp.json()["spent"] == pytest.approx(0.04)
    assert resp.json()["remaining"] == 0.0

    # Removing the cap unblocks the team.
    await client.delete(f"/teams/{team}/budget", headers=_bearer(admin))
    assert (await _chat(client, key)).status_code == HTTP_200_OK


async def test_only_platform_admin_can_set_or_remove_budget(client: AsyncTestClient) -> None:
    _, team, admin = await _setup_team(client)

    # Onboard a regular user (invite → signup → login) and make them team admin:
    # they manage the team but must NOT be able to raise their own spend cap.
    invite = await seed_team_and_invite(client, admin)
    await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": "dev@example.com",
            "password": DEV_PASSWORD,
        },
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "dev@example.com", "role": "admin"},
        headers=_bearer(admin),
    )
    dev_login = await client.post(
        "/login", json={"email": "dev@example.com", "password": DEV_PASSWORD}
    )
    dev = dev_login.json()["access_token"]

    resp = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 999.0, "window": "monthly"},
        headers=_bearer(dev),
    )
    assert resp.status_code == HTTP_403_FORBIDDEN

    resp = await client.delete(f"/teams/{team}/budget", headers=_bearer(dev))
    assert resp.status_code == HTTP_403_FORBIDDEN

    # But a team admin can view the budget once one exists.
    await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 1.0, "window": "monthly"},
        headers=_bearer(admin),
    )
    resp = await client.get(f"/teams/{team}/budget", headers=_bearer(dev))
    assert resp.status_code == HTTP_200_OK
    assert resp.json()["limit_cost"] == 1.0
