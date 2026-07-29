"""Plan 18 Phase 1 — the egress allowlist gates `openai_compatible` credentials.

Carries the acceptance criterion the plan originally placed in Phase 0, which
needed the provider value introduced here.

Literal IPs throughout: `resolve_allowlisted_addresses` short-circuits DNS for
a literal, so these tests never touch the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
SALT_KEY = "unit-test-salt-key"  # pragma: allowlist secret


def _settings(database_url: str, allowed: tuple[str, ...]) -> Settings:
    return Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
        openai_compatible_allowed_hosts=allowed,
    )


@pytest.fixture
async def locked_down(database_url: str) -> AsyncIterator[AsyncTestClient]:
    """No allowlist configured — the default for every deployment that upgrades."""
    async with AsyncTestClient(app=create_app(_settings(database_url, ()))) as client:
        yield client


@pytest.fixture
async def opted_in(database_url: str) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(
        app=create_app(_settings(database_url, ("10.42.0.0/16",)))
    ) as client:
        yield client


async def _admin_headers(client: AsyncTestClient) -> dict[str, str]:
    token = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _create(
    client: AsyncTestClient, api_base: str, name: str = "local-vllm"
) -> tuple[int, str]:
    response = await client.post(
        "/credentials",
        json={
            "name": name,
            "provider": "openai_compatible",
            "values": {"api_base": api_base},
        },
        headers=await _admin_headers(client),
    )
    return response.status_code, response.text


async def test_an_empty_allowlist_refuses_every_target(locked_down: AsyncTestClient) -> None:
    status, body = await _create(locked_down, "http://10.42.0.9:8000/v1")
    assert status == HTTP_400_BAD_REQUEST
    # The message must name the setting, or an operator cannot act on it.
    assert "OPENAI_COMPATIBLE_ALLOWED_HOSTS" in body


async def test_an_allowlisted_target_is_accepted(opted_in: AsyncTestClient) -> None:
    status, body = await _create(opted_in, "http://10.42.0.9:8000/v1")
    assert status == HTTP_201_CREATED, body


async def test_a_target_outside_the_allowlist_is_refused(opted_in: AsyncTestClient) -> None:
    status, body = await _create(opted_in, "http://169.254.169.254/v1")
    assert status == HTTP_400_BAD_REQUEST
    assert "not permitted" in body


async def test_credentials_embedded_in_the_url_are_refused(opted_in: AsyncTestClient) -> None:
    # `ClientKey.endpoint` holds `api_base` in the clear on purpose — it is a
    # metric label and appears in registry log lines — so a password in the URL
    # would be logged verbatim. This is the first provider whose only required
    # field is a URL and whose `api_key` is optional, which makes
    # `user:pass@host` the natural thing for an operator to paste.
    url = "http://operator:pw-must-not-leak@10.42.0.9:8000/v1"  # pragma: allowlist secret
    status, body = await _create(opted_in, url)
    assert status == HTTP_400_BAD_REQUEST
    # The host itself is allowlisted, so only the userinfo can be the reason.
    assert "userinfo" in body
    # The refusal must not echo back the very value it is refusing.
    assert "pw-must-not-leak" not in body


async def test_a_non_http_scheme_is_refused(opted_in: AsyncTestClient) -> None:
    status, _ = await _create(opted_in, "file:///etc/passwd")
    assert status == HTTP_400_BAD_REQUEST


async def test_api_base_is_required(opted_in: AsyncTestClient) -> None:
    response = await opted_in.post(
        "/credentials",
        json={"name": "no-endpoint", "provider": "openai_compatible", "values": {"api_key": "k"}},
        headers=await _admin_headers(opted_in),
    )
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert "api_base" in response.text


async def test_update_cannot_move_the_endpoint_off_the_allowlist(
    opted_in: AsyncTestClient,
) -> None:
    headers = await _admin_headers(opted_in)
    created = await opted_in.post(
        "/credentials",
        json={
            "name": "movable",
            "provider": "openai_compatible",
            "values": {"api_base": "http://10.42.0.9:8000/v1"},
        },
        headers=headers,
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    credential_id = created.json()["id"]

    moved = await opted_in.patch(
        f"/credentials/{credential_id}",
        json={"values": {"api_base": "http://169.254.169.254/v1"}},
        headers=headers,
    )
    assert moved.status_code == HTTP_400_BAD_REQUEST
    assert "not permitted" in moved.text
