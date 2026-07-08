"""SCIM Users: PUT/PATCH updates, deactivate-on-DELETE, and the admin guard."""

from __future__ import annotations

from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient

from .conftest import (
    ADMIN_EMAIL,
    ERROR_URN,
    USER_URN,
    _admin_headers,
    _create_user,
    _scim_headers,
    _signup_password_user,
)


async def test_put_replaces_username_external_id_and_active(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    created = await _create_user(client, headers, "alice@corp.com", external_id="aad-1")

    resp = await client.put(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": [USER_URN],
            "userName": "alice.smith@corp.com",
            "externalId": "aad-1b",
            "active": False,
        },
        headers=headers,
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["userName"] == "alice.smith@corp.com"
    assert body["externalId"] == "aad-1b"
    assert body["active"] is False


async def test_patch_deactivate_revokes_existing_sessions(client: AsyncTestClient) -> None:
    await _signup_password_user(client, "carol@corp.com", "Sup3r-Secret!")
    login = await client.post(
        "/login",
        json={"email": "carol@corp.com", "password": "Sup3r-Secret!"},  # pragma: allowlist secret
    )
    user_jwt = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/me", headers=user_jwt)).status_code == HTTP_200_OK

    headers = await _scim_headers(client)
    found = (
        await client.get('/scim/v2/Users?filter=userName eq "carol@corp.com"', headers=headers)
    ).json()
    user_id = found["Resources"][0]["id"]

    patched = await client.patch(
        f"/scim/v2/Users/{user_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
        headers=headers,
    )
    assert patched.status_code == HTTP_200_OK, patched.text
    assert patched.json()["active"] is False

    # The pre-deactivation JWT no longer authenticates, and login is rejected.
    assert (await client.get("/me", headers=user_jwt)).status_code == HTTP_401_UNAUTHORIZED
    relogin = await client.post(
        "/login",
        json={"email": "carol@corp.com", "password": "Sup3r-Secret!"},  # pragma: allowlist secret
    )
    assert relogin.status_code == HTTP_401_UNAUTHORIZED


async def test_patch_entra_style_no_path_with_string_bool(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    created = await _create_user(client, headers, "dave@corp.com", external_id="aad-9")

    patched = await client.patch(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "Replace", "value": {"active": "False"}}],
        },
        headers=headers,
    )
    assert patched.status_code == HTTP_200_OK, patched.text
    assert patched.json()["active"] is False


async def test_patch_reactivate_allows_login_again(client: AsyncTestClient) -> None:
    await _signup_password_user(client, "erin@corp.com", "Sup3r-Secret!")
    headers = await _scim_headers(client)
    found = (
        await client.get('/scim/v2/Users?filter=userName eq "erin@corp.com"', headers=headers)
    ).json()
    user_id = found["Resources"][0]["id"]

    def patch_active(value: bool) -> dict:
        return {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": value}],
        }

    await client.patch(f"/scim/v2/Users/{user_id}", json=patch_active(False), headers=headers)
    reactivated = await client.patch(
        f"/scim/v2/Users/{user_id}", json=patch_active(True), headers=headers
    )
    assert reactivated.json()["active"] is True

    relogin = await client.post(
        "/login",
        json={"email": "erin@corp.com", "password": "Sup3r-Secret!"},  # pragma: allowlist secret
    )
    assert relogin.status_code == HTTP_200_OK


async def test_patch_username_conflict_is_scim_409(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    await _create_user(client, headers, "alice@corp.com", external_id="e1")
    other = await _create_user(client, headers, "bob@corp.com", external_id="e2")

    resp = await client.patch(
        f"/scim/v2/Users/{other['id']}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "userName", "value": "alice@corp.com"}],
        },
        headers=headers,
    )
    assert resp.status_code == HTTP_409_CONFLICT
    assert resp.json()["scimType"] == "uniqueness"


async def test_delete_deactivates_instead_of_deleting(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    created = await _create_user(client, headers, "frank@corp.com")

    resp = await client.delete(f"/scim/v2/Users/{created['id']}", headers=headers)
    assert resp.status_code == HTTP_204_NO_CONTENT

    fetched = await client.get(f"/scim/v2/Users/{created['id']}", headers=headers)
    assert fetched.status_code == HTTP_200_OK
    assert fetched.json()["active"] is False


async def test_scim_cannot_reactivate_admin_disabled_account(client: AsyncTestClient) -> None:
    await _signup_password_user(client, "hank@corp.com", "Sup3r-Secret!")
    admin = await _admin_headers(client)
    headers = await _scim_headers(client)
    found = (
        await client.get('/scim/v2/Users?filter=userName eq "hank@corp.com"', headers=headers)
    ).json()
    user_id = found["Resources"][0]["id"]

    def patch_active(value: bool) -> dict:
        return {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": value}],
        }

    disabled = await client.patch(f"/users/{user_id}", json={"is_active": False}, headers=admin)
    assert disabled.status_code == HTTP_200_OK, disabled.text

    # A routine IdP full-sync with active:true must not resurrect the account.
    for request in (
        client.patch(f"/scim/v2/Users/{user_id}", json=patch_active(True), headers=headers),
        client.put(
            f"/scim/v2/Users/{user_id}",
            json={"schemas": [USER_URN], "userName": "hank@corp.com", "active": True},
            headers=headers,
        ),
    ):
        resp = await request
        assert resp.status_code == HTTP_403_FORBIDDEN, resp.text
        assert resp.json()["schemas"] == [ERROR_URN]

    # The admin lever is unaffected: re-enable via the admin API...
    reenabled = await client.patch(f"/users/{user_id}", json={"is_active": True}, headers=admin)
    assert reenabled.status_code == HTTP_200_OK, reenabled.text
    # ...after which the IdP governs the account again.
    redisabled = await client.patch(
        f"/scim/v2/Users/{user_id}", json=patch_active(False), headers=headers
    )
    assert redisabled.status_code == HTTP_200_OK, redisabled.text


async def test_scim_cannot_deactivate_platform_admin(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    found = (
        await client.get(f'/scim/v2/Users?filter=userName eq "{ADMIN_EMAIL}"', headers=headers)
    ).json()
    admin_id = found["Resources"][0]["id"]

    for request in (
        client.patch(
            f"/scim/v2/Users/{admin_id}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=headers,
        ),
        client.delete(f"/scim/v2/Users/{admin_id}", headers=headers),
    ):
        resp = await request
        assert resp.status_code == HTTP_403_FORBIDDEN
        assert resp.json()["schemas"] == [ERROR_URN]
