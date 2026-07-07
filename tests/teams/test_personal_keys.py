"""Personal keys (`POST /teams/{id}/keys`): inference-only, revoked on deactivation."""

from __future__ import annotations

from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from .conftest import DEV_PASSWORD, _admin, _bearer, _model, _team_and_credential


async def test_personal_key_is_inference_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    resp = await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    assert resp.status_code == HTTP_201_CREATED
    assert resp.json()["scope"] == "inference"


async def test_personal_key_cannot_request_management(client: AsyncTestClient) -> None:
    # Management belongs to a service principal, not a personal key.
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    resp = await client.post(
        f"/teams/{team}/keys", json={"name": "k", "scope": "management"}, headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_personal_keys_revoked_when_user_deactivated(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    # Onboard a member and give them a personal (inference) key.
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "dev@example.com", "password": DEV_PASSWORD},
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "dev@example.com", "role": "admin"},
        headers=_bearer(admin),
    )
    dev = (
        await client.post("/login", json={"email": "dev@example.com", "password": DEV_PASSWORD})
    ).json()["access_token"]
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(admin))
    # A personal key created by the dev — owned/attributed to them (created_by).
    dev_key = (
        await client.post(f"/teams/{team}/keys", json={"name": "devk"}, headers=_bearer(dev))
    ).json()["plaintext"]

    # Works before deactivation.
    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(dev_key),
    )
    assert ok.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)

    # Find the dev's user id and disable them.
    members = (await client.get(f"/teams/{team}/members", headers=_bearer(admin))).json()
    admin_id = (await client.get("/me", headers=_bearer(admin))).json()["id"]
    dev_id = next(m["user_id"] for m in members if m["user_id"] != admin_id)
    await client.patch(f"/users/{dev_id}", json={"is_active": False}, headers=_bearer(admin))

    # The personal key is now revoked.
    denied = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(dev_key),
    )
    assert denied.status_code == HTTP_401_UNAUTHORIZED
