"""The guardrail management API end to end.

Two things this covers that the service tests cannot: that a signing secret
never appears in any response body on any route, and that the roles which can
manage models cannot quietly switch a content control off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient
from rbac.conftest import (  # type: ignore[import-not-found]
    _admin,
    _bearer,
    _credential,
    _member_token,
    _model_payload,
    _team,
)

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"
SIGNING_MATERIAL = "webhook-signing-material"  # pragma: allowlist secret
WEBHOOK_URL = "https://scanner.example/check"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _webhook_payload(name: str = "scanner", **overrides) -> dict:
    payload = {
        "name": name,
        "kind": "webhook",
        "direction": "request",
        "fail_policy": "closed",
        "config": {"url": WEBHOOK_URL, "timeout_ms": 1500},
        "signing_secret": SIGNING_MATERIAL,
    }
    payload.update(overrides)
    return payload


async def _create(client: AsyncTestClient, token: str, team: str, **overrides):
    return await client.post(
        f"/teams/{team}/guardrails",
        json=_webhook_payload(**overrides),
        headers=_bearer(token),
    )


# ── The secret never comes back ───────────────────────────────────────────────


async def test_no_route_ever_returns_the_signing_secret(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    created = await _create(client, admin, team)
    assert created.status_code == HTTP_201_CREATED, created.text
    rule = created.json()
    assert rule["has_secret"] is True
    assert SIGNING_MATERIAL not in created.text

    listed = await client.get(f"/teams/{team}/guardrails", headers=_bearer(admin))
    fetched = await client.get(f"/teams/{team}/guardrails/{rule['id']}", headers=_bearer(admin))
    patched = await client.patch(
        f"/teams/{team}/guardrails/{rule['id']}",
        json={"position": 3},
        headers=_bearer(admin),
    )

    assert listed.status_code == fetched.status_code == patched.status_code == HTTP_200_OK
    for response in (listed, fetched, patched):
        assert SIGNING_MATERIAL not in response.text
        assert "signing_secret" not in response.text
    # Editing something else did not drop the secret — which would silently
    # unsign the endpoint.
    assert patched.json()["has_secret"] is True
    assert patched.json()["position"] == 3


# ── Authorization ─────────────────────────────────────────────────────────────


async def test_a_model_manager_cannot_touch_the_guardrail_policy(
    client: AsyncTestClient,
) -> None:
    # The role that configures models is deliberately not the role that decides
    # what content may leave: a control the model owner can switch off is not a
    # control.
    admin = await _admin(client)
    team = await _team(client, admin)
    created = await _create(client, admin, team)
    rule_id = created.json()["id"]
    manager = await _member_token(client, admin, team, "mm@example.com", "model-manager")

    # Sanity: this token really can manage models in this team.
    cred = await _credential(client, admin)
    allowed = await client.post(
        f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(manager)
    )
    assert allowed.status_code == HTTP_201_CREATED, allowed.text

    for response in (
        await client.get(f"/teams/{team}/guardrails", headers=_bearer(manager)),
        await _create(client, manager, team, name="sneaky"),
        await client.patch(
            f"/teams/{team}/guardrails/{rule_id}",
            json={"enabled": False},
            headers=_bearer(manager),
        ),
        await client.delete(f"/teams/{team}/guardrails/{rule_id}", headers=_bearer(manager)),
    ):
        assert response.status_code == HTTP_403_FORBIDDEN, response.text

    # And the rule is untouched.
    still = await client.get(f"/teams/{team}/guardrails/{rule_id}", headers=_bearer(admin))
    assert still.json()["enabled"] is True


async def test_a_plain_member_cannot_read_the_policy(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "member@example.com", "member")

    response = await client.get(f"/teams/{team}/guardrails", headers=_bearer(member))
    assert response.status_code == HTTP_403_FORBIDDEN


async def test_another_teams_admin_cannot_read_or_write(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    other_team = await _team(client, admin)
    outsider = await _member_token(client, admin, other_team, "out@example.com", "admin")

    assert (
        await client.get(f"/teams/{team}/guardrails", headers=_bearer(outsider))
    ).status_code == HTTP_403_FORBIDDEN
    assert (await _create(client, outsider, team)).status_code == HTTP_403_FORBIDDEN


# ── Validation surfaced as 4xx ────────────────────────────────────────────────


async def test_a_cleartext_webhook_is_refused(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    response = await _create(client, admin, team, config={"url": "http://scanner.example/check"})

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert "https" in response.text


async def test_a_webhook_without_a_secret_is_refused(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    payload = _webhook_payload()
    del payload["signing_secret"]
    response = await client.post(f"/teams/{team}/guardrails", json=payload, headers=_bearer(admin))

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert "signing secret" in response.text


async def test_an_unknown_config_key_is_refused_not_ignored(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    response = await _create(client, admin, team, config={"url": WEBHOOK_URL, "timout_ms": 500})

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert "timout_ms" in response.text


async def test_an_unknown_kind_or_direction_is_a_bad_request(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    for overrides in ({"kind": "telepathy"}, {"direction": "sideways"}):
        response = await _create(client, admin, team, **overrides)
        assert response.status_code == HTTP_400_BAD_REQUEST, response.text


async def test_a_duplicate_name_is_a_bad_request(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    await _create(client, admin, team)

    response = await _create(client, admin, team)

    assert response.status_code == HTTP_400_BAD_REQUEST
    assert "already exists" in response.text


async def test_a_model_scoped_rule_must_name_a_model_of_the_team(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team, other_team = await _team(client, admin), await _team(client, admin)
    cred = await _credential(client, admin)
    foreign = await client.post(
        f"/teams/{other_team}/models", json=_model_payload(cred), headers=_bearer(admin)
    )

    response = await _create(client, admin, team, model_id=foreign.json()["id"])

    assert response.status_code == HTTP_404_NOT_FOUND, response.text


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def test_delete_removes_the_rule(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    rule_id = (await _create(client, admin, team)).json()["id"]

    removed = await client.delete(f"/teams/{team}/guardrails/{rule_id}", headers=_bearer(admin))
    assert removed.status_code == HTTP_204_NO_CONTENT

    assert (
        await client.get(f"/teams/{team}/guardrails/{rule_id}", headers=_bearer(admin))
    ).status_code == HTTP_404_NOT_FOUND
    assert (
        await client.delete(f"/teams/{team}/guardrails/{rule_id}", headers=_bearer(admin))
    ).status_code == HTTP_404_NOT_FOUND


async def test_rules_are_listed_in_chain_order(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    await _create(client, admin, team, name="second", position=2)
    await _create(client, admin, team, name="first", position=1)

    listed = await client.get(f"/teams/{team}/guardrails", headers=_bearer(admin))

    assert [r["name"] for r in listed.json()] == ["first", "second"]
