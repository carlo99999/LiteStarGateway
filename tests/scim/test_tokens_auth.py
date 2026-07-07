"""Provisioning-token minting + SCIM bearer-token authentication."""

from __future__ import annotations

from conftest import _admin_headers, _scim_headers
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
)
from litestar.testing import AsyncTestClient


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


async def test_service_provider_config(client: AsyncTestClient) -> None:
    headers = await _scim_headers(client)
    resp = await client.get("/scim/v2/ServiceProviderConfig", headers=headers)
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True
    assert body["bulk"]["supported"] is False
    assert body["changePassword"]["supported"] is False
