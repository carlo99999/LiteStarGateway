"""Personal keys (`POST /teams/{id}/keys`): inference-only, revoked on deactivation."""

from __future__ import annotations

from _invite_helpers import issue_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from .conftest import DEV_PASSWORD, _admin, _bearer, _model, _team_and_credential


async def _create_personal_key_for_member(
    client: AsyncTestClient, admin: str, team: str
) -> tuple[str, str, dict]:
    invite = await issue_invite(client, admin, team, role="admin")
    signup = await client.post(
        "/signup",
        json={"invite_token": invite, "email": "dev@example.com", "password": DEV_PASSWORD},
    )
    assert signup.status_code == HTTP_201_CREATED, signup.text
    login = await client.post("/login", json={"email": "dev@example.com", "password": DEV_PASSWORD})
    assert login.status_code == HTTP_200_OK, login.text
    token = login.json()["access_token"]
    created = await client.post(
        f"/teams/{team}/keys", json={"name": "devk"}, headers=_bearer(token)
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    return signup.json()["id"], token, created.json()


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
    dev_id, _, created = await _create_personal_key_for_member(client, admin, team)
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(admin))
    dev_key = created["plaintext"]

    # Works before deactivation.
    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(dev_key),
    )
    assert ok.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)

    await client.patch(f"/users/{dev_id}", json={"is_active": False}, headers=_bearer(admin))

    # The personal key is now revoked.
    denied = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(dev_key),
    )
    assert denied.status_code == HTTP_401_UNAUTHORIZED


async def test_admin_rotation_preserves_personal_key_owner(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    dev_id, _, created = await _create_personal_key_for_member(client, admin, team)

    rotated = await client.post(
        f"/teams/{team}/keys/{created['id']}/rotate", headers=_bearer(admin)
    )
    assert rotated.status_code == HTTP_201_CREATED, rotated.text
    keys = (await client.get(f"/teams/{team}/keys", headers=_bearer(admin))).json()
    replacement = next(key for key in keys if key["id"] == rotated.json()["id"])
    assert replacement["created_by"] == dev_id

    for plaintext in (created["plaintext"], rotated.json()["plaintext"]):
        assert (await client.get("/whoami", headers=_bearer(plaintext))).status_code == HTTP_200_OK

    disabled = await client.patch(
        f"/users/{dev_id}", json={"is_active": False}, headers=_bearer(admin)
    )
    assert disabled.status_code == HTTP_200_OK, disabled.text
    for plaintext in (created["plaintext"], rotated.json()["plaintext"]):
        assert (
            await client.get("/whoami", headers=_bearer(plaintext))
        ).status_code == HTTP_401_UNAUTHORIZED

    # Authentication also checks owner activity, so prove the persisted key
    # revocation itself was shortened from grace to immediate: re-enabling the
    # account must not resurrect either credential.
    enabled = await client.patch(
        f"/users/{dev_id}", json={"is_active": True}, headers=_bearer(admin)
    )
    assert enabled.status_code == HTTP_200_OK, enabled.text
    for plaintext in (created["plaintext"], rotated.json()["plaintext"]):
        assert (
            await client.get("/whoami", headers=_bearer(plaintext))
        ).status_code == HTTP_401_UNAUTHORIZED


async def test_deactivation_revokes_personal_key_in_rotation_grace(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    dev_id, dev, created = await _create_personal_key_for_member(client, admin, team)
    rotated = await client.post(f"/teams/{team}/keys/{created['id']}/rotate", headers=_bearer(dev))
    assert rotated.status_code == HTTP_201_CREATED, rotated.text

    for plaintext in (created["plaintext"], rotated.json()["plaintext"]):
        assert (await client.get("/whoami", headers=_bearer(plaintext))).status_code == HTTP_200_OK

    disabled = await client.patch(
        f"/users/{dev_id}", json={"is_active": False}, headers=_bearer(admin)
    )
    assert disabled.status_code == HTTP_200_OK, disabled.text
    for plaintext in (created["plaintext"], rotated.json()["plaintext"]):
        assert (
            await client.get("/whoami", headers=_bearer(plaintext))
        ).status_code == HTTP_401_UNAUTHORIZED

    enabled = await client.patch(
        f"/users/{dev_id}", json={"is_active": True}, headers=_bearer(admin)
    )
    assert enabled.status_code == HTTP_200_OK, enabled.text
    for plaintext in (created["plaintext"], rotated.json()["plaintext"]):
        assert (
            await client.get("/whoami", headers=_bearer(plaintext))
        ).status_code == HTTP_401_UNAUTHORIZED
