"""SCIM Users: create, read, filter, and paginate."""

from __future__ import annotations

import json

from conftest import ERROR_URN, LIST_URN, USER_URN, _create_user, _scim_headers
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient


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
