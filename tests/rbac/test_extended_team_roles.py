"""Extended team roles end-to-end: model-manager, key-issuer, billing-viewer,
plus member/admin unchanged and the last-admin guard against demotion."""

from __future__ import annotations

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient

from .conftest import (
    ADMIN_EMAIL,
    _admin,
    _bearer,
    _credential,
    _member_token,
    _model_payload,
    _team,
)


async def test_model_manager_manages_models_and_nothing_else(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    token = await _member_token(client, admin, team, "mm@corp.com", "model-manager")

    created = await client.post(
        f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    assert (
        await client.get(f"/teams/{team}/models", headers=_bearer(token))
    ).status_code == HTTP_200_OK

    denied = [
        client.post(
            f"/teams/{team}/members",
            json={"email": ADMIN_EMAIL, "role": "member"},
            headers=_bearer(token),
        ),
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.post(
            f"/teams/{team}/service-principals", json={"name": "sp"}, headers=_bearer(token)
        ),
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
    ]
    for request in denied:
        assert (await request).status_code == HTTP_403_FORBIDDEN


async def test_key_issuer_mints_lists_and_revokes_keys_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    token = await _member_token(client, admin, team, "ki@corp.com", "key-issuer")

    minted = await client.post(f"/teams/{team}/keys", json={"name": "app"}, headers=_bearer(token))
    assert minted.status_code == HTTP_201_CREATED, minted.text
    key_id = minted.json()["id"]
    assert (
        await client.get(f"/teams/{team}/keys", headers=_bearer(token))
    ).status_code == HTTP_200_OK
    rotated = await client.post(f"/teams/{team}/keys/{key_id}/rotate", headers=_bearer(token))
    assert rotated.status_code == HTTP_201_CREATED, rotated.text
    revoked = await client.delete(f"/teams/{team}/keys/{key_id}", headers=_bearer(token))
    assert revoked.status_code == HTTP_204_NO_CONTENT

    for request in (
        client.post(f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)),
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


@pytest.mark.parametrize("scope", ["inference", "management", "all"])
async def test_key_issuer_cannot_rotate_service_principal_key(
    client: AsyncTestClient, scope: str
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    key_issuer = await _member_token(client, admin, team, "ki-sp@corp.com", "key-issuer")

    service_principal = await client.post(
        f"/teams/{team}/service-principals",
        json={"name": "deployment-bot"},
        headers=_bearer(admin),
    )
    assert service_principal.status_code == HTTP_201_CREATED, service_principal.text
    service_principal_id = service_principal.json()["id"]
    issued = await client.post(
        f"/teams/{team}/service-principals/{service_principal_id}/keys",
        json={"name": f"{scope}-key", "scope": scope, "rate_limit_rpm": 37},
        headers=_bearer(admin),
    )
    assert issued.status_code == HTTP_201_CREATED, issued.text
    key_id = issued.json()["id"]

    keys_before = await client.get(f"/teams/{team}/keys", headers=_bearer(key_issuer))
    denied = await client.post(
        f"/teams/{team}/keys/{key_id}/rotate",
        headers=_bearer(key_issuer),
    )
    keys_after = await client.get(f"/teams/{team}/keys", headers=_bearer(key_issuer))

    assert denied.status_code == HTTP_403_FORBIDDEN
    assert keys_after.json() == keys_before.json()

    rotated = await client.post(
        f"/teams/{team}/keys/{key_id}/rotate",
        headers=_bearer(admin),
    )
    assert rotated.status_code == HTTP_201_CREATED, rotated.text
    assert rotated.json()["scope"] == scope
    assert rotated.json()["rate_limit_rpm"] == 37

    disabled = await client.patch(
        f"/teams/{team}/service-principals/{service_principal_id}",
        json={"enabled": False},
        headers=_bearer(admin),
    )
    assert disabled.status_code == HTTP_200_OK, disabled.text
    assert (
        await client.get("/whoami", headers=_bearer(rotated.json()["plaintext"]))
    ).status_code == HTTP_401_UNAUTHORIZED


async def test_billing_viewer_reads_usage_budget_and_spend_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    set_budget = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 100.0, "window": "monthly"},
        headers=_bearer(admin),
    )
    assert set_budget.status_code in (HTTP_200_OK, HTTP_201_CREATED), set_budget.text
    token = await _member_token(client, admin, team, "bv@corp.com", "billing-viewer")

    for request in (
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.get(f"/teams/{team}/budget", headers=_bearer(token)),
        client.get(f"/teams/{team}/keys/spending", headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_200_OK

    for request in (
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
        client.post(f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)),
        client.put(
            f"/teams/{team}/budget",
            json={"limit_cost": 1.0, "window": "monthly"},
            headers=_bearer(token),
        ),
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


async def test_billing_viewer_spending_redacts_key_identity(client: AsyncTestClient) -> None:
    """usage:read alone yields spend figures with the key inventory redacted;
    keys:read (here via team admin) unlocks the identity fields (R6-M43)."""
    admin = await _admin(client)
    team = await _team(client, admin)
    minted = await client.post(f"/teams/{team}/keys", json={"name": "app"}, headers=_bearer(admin))
    assert minted.status_code == HTTP_201_CREATED, minted.text
    token = await _member_token(client, admin, team, "bv@corp.com", "billing-viewer")

    rows = (await client.get(f"/teams/{team}/keys/spending", headers=_bearer(token))).json()
    assert len(rows) == 1
    assert rows[0]["id"] == minted.json()["id"]
    assert rows[0]["calls"] == 0
    assert rows[0]["cost"] == 0.0
    for field in ("name", "prefix", "is_active", "created_at", "revoked_at"):
        assert rows[0][field] is None

    # The admin holds keys:read as well, so the identity block stays visible.
    full = (await client.get(f"/teams/{team}/keys/spending", headers=_bearer(admin))).json()
    assert full[0]["name"] == "app"
    assert full[0]["prefix"] == minted.json()["prefix"]
    assert full[0]["is_active"] is True
    assert full[0]["created_at"] is not None


async def test_plain_member_is_still_denied_all_management(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    token = await _member_token(client, admin, team, "plain@corp.com", "member")

    for request in (
        client.get(f"/teams/{team}/members", headers=_bearer(token)),
        client.get(f"/teams/{team}/models", headers=_bearer(token)),
        client.get(f"/teams/{team}/keys", headers=_bearer(token)),
        client.get(f"/teams/{team}/usage", headers=_bearer(token)),
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


async def test_team_admin_keeps_full_access(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    token = await _member_token(client, admin, team, "lead@corp.com", "admin")

    assert (
        await client.post(
            f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)
        )
    ).status_code == HTTP_201_CREATED
    assert (
        await client.get(f"/teams/{team}/members", headers=_bearer(token))
    ).status_code == HTTP_200_OK
    assert (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token))
    ).status_code == HTTP_201_CREATED


async def test_last_admin_cannot_be_demoted_to_extended_role(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    admin_id = (await client.get("/me", headers=_bearer(admin))).json()["id"]

    resp = await client.patch(
        f"/teams/{team}/members/{admin_id}",
        json={"role": "billing-viewer"},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_409_CONFLICT
