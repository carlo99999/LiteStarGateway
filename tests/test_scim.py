"""SCIM 2.0 provisioning: admin-minted tokens + /scim/v2/Users lifecycle."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.web.scim.schemas import (
    ScimUserAttrs,
    apply_patch_ops,
    parse_filter,
)

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"

ERROR_URN = "urn:ietf:params:scim:api:messages:2.0:Error"
LIST_URN = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'scim.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    app = create_app(_settings(tmp_path))
    async with AsyncTestClient(app=app) as test_client:
        yield test_client


async def _admin_headers(client: AsyncTestClient) -> dict[str, str]:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    assert resp.status_code == HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _mint_token(client: AsyncTestClient, name: str = "entra") -> str:
    resp = await client.post(
        "/scim-tokens", json={"name": name}, headers=await _admin_headers(client)
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["token"]


async def _scim_headers(client: AsyncTestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _mint_token(client)}"}


async def _create_user(
    client: AsyncTestClient,
    headers: dict[str, str],
    email: str,
    external_id: str | None = "ext-1",
) -> dict:
    body: dict = {"schemas": [USER_URN], "userName": email, "active": True}
    if external_id is not None:
        body["externalId"] = external_id
    resp = await client.post("/scim/v2/Users", json=body, headers=headers)
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()


async def _signup_password_user(client: AsyncTestClient, email: str, password: str) -> None:
    invite = await client.post("/invites", headers=await _admin_headers(client))
    assert invite.status_code == HTTP_201_CREATED, invite.text
    resp = await client.post(
        "/signup",
        json={"invite_token": invite.json()["token"], "email": email, "password": password},
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text


# ── Token minting & SCIM authentication ──────────────────────────────────────


async def test_mint_scim_token_requires_admin_jwt(client: AsyncTestClient) -> None:
    resp = await client.post("/scim-tokens", json={"name": "x"})
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_minted_token_is_returned_once_and_listed_without_plaintext(
    client: AsyncTestClient,
) -> None:
    headers = await _admin_headers(client)
    created = await client.post("/scim-tokens", json={"name": "entra"}, headers=headers)
    assert created.status_code == HTTP_201_CREATED
    body = created.json()
    assert body["token"].startswith("scim_")
    assert body["name"] == "entra"

    listed = await client.get("/scim-tokens", headers=headers)
    assert listed.status_code == HTTP_200_OK
    (row,) = listed.json()
    assert row["name"] == "entra"
    assert row["revoked_at"] is None
    assert "token" not in row and "token_hash" not in row


async def test_scim_rejects_missing_or_unknown_token(client: AsyncTestClient) -> None:
    assert (await client.get("/scim/v2/Users")).status_code == HTTP_401_UNAUTHORIZED
    resp = await client.get(
        "/scim/v2/Users", headers={"Authorization": "Bearer scim_not-a-real-token"}
    )
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_scim_rejects_user_jwt_and_revoked_token(client: AsyncTestClient) -> None:
    # A login JWT is not a provisioning token.
    resp = await client.get("/scim/v2/Users", headers=await _admin_headers(client))
    assert resp.status_code == HTTP_401_UNAUTHORIZED

    admin = await _admin_headers(client)
    created = (await client.post("/scim-tokens", json={"name": "old"}, headers=admin)).json()
    scim = {"Authorization": f"Bearer {created['token']}"}
    assert (await client.get("/scim/v2/Users", headers=scim)).status_code == HTTP_200_OK

    revoke = await client.delete(f"/scim-tokens/{created['id']}", headers=admin)
    assert revoke.status_code == HTTP_204_NO_CONTENT
    assert (await client.get("/scim/v2/Users", headers=scim)).status_code == HTTP_401_UNAUTHORIZED


# ── ServiceProviderConfig ────────────────────────────────────────────────────


async def test_service_provider_config(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    resp = await client.get("/scim/v2/ServiceProviderConfig", headers=headers)
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True
    assert body["bulk"]["supported"] is False
    assert body["changePassword"]["supported"] is False


# ── Users: create / read / list ──────────────────────────────────────────────


async def test_create_user_returns_scim_resource(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    body = await _create_user(client, headers, "alice@corp.com", external_id="aad-1")
    assert body["schemas"] == [USER_URN]
    assert body["userName"] == "alice@corp.com"
    assert body["externalId"] == "aad-1"
    assert body["active"] is True
    assert body["meta"]["resourceType"] == "User"
    assert body["meta"]["location"].endswith(f"/scim/v2/Users/{body['id']}")

    fetched = await client.get(f"/scim/v2/Users/{body['id']}", headers=headers)
    assert fetched.status_code == HTTP_200_OK
    assert fetched.json()["userName"] == "alice@corp.com"
    assert fetched.headers["content-type"].startswith("application/scim+json")


async def test_create_accepts_scim_json_content_type(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    resp = await client.post(
        "/scim/v2/Users",
        content=json.dumps({"schemas": [USER_URN], "userName": "bob@corp.com", "active": True}),
        headers={**headers, "Content-Type": "application/scim+json"},
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text


async def test_create_duplicate_username_is_scim_conflict(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    await _create_user(client, headers, "alice@corp.com")
    resp = await client.post(
        "/scim/v2/Users",
        json={"schemas": [USER_URN], "userName": "Alice@corp.com", "active": True},
        headers=headers,
    )
    assert resp.status_code == HTTP_409_CONFLICT
    body = resp.json()
    assert body["schemas"] == [ERROR_URN]
    assert body["status"] == "409"
    assert body["scimType"] == "uniqueness"


async def test_create_without_username_is_bad_request(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    resp = await client.post(
        "/scim/v2/Users", json={"schemas": [USER_URN], "active": True}, headers=headers
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST
    assert resp.json()["schemas"] == [ERROR_URN]


async def test_get_unknown_user_is_scim_404(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    resp = await client.get("/scim/v2/Users/00000000-0000-0000-0000-000000000000", headers=headers)
    assert resp.status_code == HTTP_404_NOT_FOUND
    body = resp.json()
    assert body["schemas"] == [ERROR_URN]
    assert body["status"] == "404"


async def test_filter_by_username_and_external_id(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    created = await _create_user(client, headers, "alice@corp.com", external_id="aad-1")

    by_name = await client.get(
        '/scim/v2/Users?filter=userName eq "alice@corp.com"', headers=headers
    )
    assert by_name.status_code == HTTP_200_OK
    body = by_name.json()
    assert body["schemas"] == [LIST_URN]
    assert body["totalResults"] == 1
    assert body["Resources"][0]["id"] == created["id"]

    by_ext = await client.get('/scim/v2/Users?filter=externalId eq "aad-1"', headers=headers)
    assert by_ext.json()["totalResults"] == 1

    missing = await client.get(
        '/scim/v2/Users?filter=userName eq "nobody@corp.com"', headers=headers
    )
    assert missing.json()["totalResults"] == 0
    assert missing.json()["Resources"] == []


async def test_unsupported_filter_is_scim_400(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    resp = await client.get('/scim/v2/Users?filter=emails co "corp"', headers=headers)
    assert resp.status_code == HTTP_400_BAD_REQUEST
    body = resp.json()
    assert body["schemas"] == [ERROR_URN]
    assert body["scimType"] == "invalidFilter"


async def test_list_pagination(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    await _create_user(client, headers, "u1@corp.com", external_id="e1")
    await _create_user(client, headers, "u2@corp.com", external_id="e2")

    # The bootstrap admin exists too: 3 users in total.
    full = (await client.get("/scim/v2/Users", headers=headers)).json()
    assert full["totalResults"] == 3
    assert len(full["Resources"]) == 3
    assert full["startIndex"] == 1

    page = (await client.get("/scim/v2/Users?startIndex=2&count=1", headers=headers)).json()
    assert page["totalResults"] == 3
    assert page["itemsPerPage"] == 1
    assert len(page["Resources"]) == 1
    assert page["startIndex"] == 2


# ── Users: update / deactivate ───────────────────────────────────────────────


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


# ── Audit trail ──────────────────────────────────────────────────────────────


async def test_scim_actions_are_audited(client: AsyncTestClient) -> None:
    admin = await _admin_headers(client)
    token = (await client.post("/scim-tokens", json={"name": "okta"}, headers=admin)).json()
    scim = {"Authorization": f"Bearer {token['token']}"}
    created = await _create_user(client, scim, "gina@corp.com")
    await client.delete(f"/scim/v2/Users/{created['id']}", headers=scim)

    events = (await client.get("/audit", headers=admin)).json()
    actions = {e["action"] for e in events}
    assert "scim_token.create" in actions
    assert "scim.user.create" in actions
    assert "scim.user.deactivate" in actions
    scim_created = next(e for e in events if e["action"] == "scim.user.create")
    assert scim_created["actor_type"] == "scim_token"
    assert scim_created["actor_email"] == "scim:okta"


# ── Pure helpers: filter parsing + PATCH op application ──────────────────────


def test_parse_filter_supported_attributes() -> None:
    assert parse_filter('userName eq "a@b.com"') == ("userName", "a@b.com")
    assert parse_filter('externalId eq "x-1"') == ("externalId", "x-1")
    # Attribute names are case-insensitive in SCIM.
    assert parse_filter('USERNAME eq "a@b.com"') == ("userName", "a@b.com")


def test_parse_filter_rejects_unsupported_expressions() -> None:
    for expression in ('emails co "corp"', 'userName ne "x"', "userName eq unquoted", ""):
        with pytest.raises(ValueError, match="filter"):
            parse_filter(expression)


def test_apply_patch_ops_with_path_and_value_dict() -> None:
    attrs = ScimUserAttrs(user_name="a@b.com", external_id="e1", active=True)

    with_path = apply_patch_ops(attrs, [{"op": "replace", "path": "active", "value": False}])
    assert with_path == ScimUserAttrs(user_name="a@b.com", external_id="e1", active=False)
    assert attrs.active is True  # input untouched

    no_path = apply_patch_ops(
        attrs,
        [{"op": "Replace", "value": {"userName": "c@d.com", "active": "False"}}],
    )
    assert no_path == ScimUserAttrs(user_name="c@d.com", external_id="e1", active=False)


def test_apply_patch_ops_ignores_unknown_paths_and_rejects_bad_ops() -> None:
    attrs = ScimUserAttrs(user_name="a@b.com", external_id=None, active=True)
    unchanged = apply_patch_ops(attrs, [{"op": "replace", "path": "displayName", "value": "Alice"}])
    assert unchanged == attrs

    with pytest.raises(ValueError, match="op"):
        apply_patch_ops(attrs, [{"op": "remove", "path": "active"}])
