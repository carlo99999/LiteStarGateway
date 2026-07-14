"""Browser-only cookie sessions and CSRF isolation from bearer/API-key auth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

ADMIN_EMAIL = "admin@example.com"
MASTER_KEY = "master-secret"  # pragma: allowlist secret
ORIGIN = "http://testserver.local"
SESSION_COOKIE = "lsg_session"
SECURE_SESSION_COOKIE = "__Host-lsg_session"


def _settings(tmp_path: Path, *, secure: bool = False) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'browser-session.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        session_cookie_secure=secure,
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as test_client:
        yield test_client


async def _browser_login(client: AsyncTestClient) -> dict[str, object]:
    response = await client.post(
        "/session/login",
        json={"email": ADMIN_EMAIL, "password": MASTER_KEY},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == HTTP_200_OK, response.text
    return response.json()


async def _bearer(client: AsyncTestClient) -> str:
    response = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    assert response.status_code == HTTP_200_OK
    return response.json()["access_token"]


async def test_browser_login_sets_http_only_cookie_without_returning_jwt(
    client: AsyncTestClient,
) -> None:
    body = await _browser_login(client)

    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie
    assert "access_token" not in body
    assert body["csrf_token"]
    assert body["expires_in"] == 7 * 24 * 60 * 60
    assert body["user"]["email"] == ADMIN_EMAIL  # type: ignore[index]


async def test_browser_login_cookie_has_hardened_attributes(client: AsyncTestClient) -> None:
    response = await client.post(
        "/session/login",
        json={"email": ADMIN_EMAIL, "password": MASTER_KEY},
        headers={"Origin": ORIGIN},
    )

    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=strict" in cookie.casefold()
    assert "Path=/" in cookie
    assert "Max-Age=604800" in cookie
    assert "Secure" not in cookie


async def test_secure_browser_cookie_uses_host_prefix(tmp_path: Path) -> None:
    async with AsyncTestClient(app=create_app(_settings(tmp_path, secure=True))) as client:
        response = await client.post(
            "/session/login",
            json={"email": ADMIN_EMAIL, "password": MASTER_KEY},
            headers={"Origin": ORIGIN},
        )

    cookie = response.headers["set-cookie"]
    assert f"{SECURE_SESSION_COOKIE}=" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Domain=" not in cookie


async def test_https_forces_secure_browser_cookie_in_local_mode(tmp_path: Path) -> None:
    async with AsyncTestClient(
        app=create_app(_settings(tmp_path)), base_url="https://testserver.local"
    ) as client:
        response = await client.post(
            "/session/login",
            json={"email": ADMIN_EMAIL, "password": MASTER_KEY},
            headers={"Origin": "https://testserver.local"},
        )

    cookie = response.headers["set-cookie"]
    assert response.status_code == HTTP_200_OK
    assert f"{SECURE_SESSION_COOKIE}=" in cookie
    assert "Secure" in cookie


async def test_browser_session_survives_reload_without_authorization(
    client: AsyncTestClient,
) -> None:
    login = await _browser_login(client)

    response = await client.get("/session")

    assert response.status_code == HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["user"]["email"] == ADMIN_EMAIL
    assert response.json()["csrf_token"] == login["csrf_token"]


async def test_cookie_mutation_requires_matching_csrf_and_origin(
    client: AsyncTestClient,
) -> None:
    login = await _browser_login(client)

    missing = await client.post("/organizations", json={"name": "Missing CSRF"})
    wrong = await client.post(
        "/organizations",
        json={"name": "Wrong CSRF"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
    )
    missing_origin = await client.post(
        "/organizations",
        json={"name": "Missing Origin"},
        headers={"X-CSRF-Token": str(login["csrf_token"])},
    )
    accepted = await client.post(
        "/organizations",
        json={"name": "Accepted"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": str(login["csrf_token"])},
    )

    assert missing.status_code == HTTP_403_FORBIDDEN
    assert wrong.status_code == HTTP_403_FORBIDDEN
    assert missing_origin.status_code == HTTP_403_FORBIDDEN
    assert accepted.status_code == HTTP_201_CREATED


async def test_cross_site_browser_login_is_rejected(client: AsyncTestClient) -> None:
    response = await client.post(
        "/session/login",
        json={"email": ADMIN_EMAIL, "password": MASTER_KEY},
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == HTTP_403_FORBIDDEN
    assert SESSION_COOKIE not in client.cookies


async def test_explicit_authorization_never_falls_back_to_cookie(
    client: AsyncTestClient,
) -> None:
    await _browser_login(client)

    response = await client.get("/me", headers={"Authorization": "Bearer invalid-explicit-token"})

    assert response.status_code == HTTP_401_UNAUTHORIZED


async def test_bearer_management_auth_remains_csrf_exempt(client: AsyncTestClient) -> None:
    await _browser_login(client)
    token = await _bearer(client)

    response = await client.post(
        "/organizations",
        json={"name": "Bearer Client"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == HTTP_201_CREATED


async def test_cookie_session_never_authenticates_inference(client: AsyncTestClient) -> None:
    await _browser_login(client)

    assert (await client.get("/whoami")).status_code == HTTP_401_UNAUTHORIZED


async def test_browser_logout_requires_csrf_then_revokes_and_clears_cookie(
    client: AsyncTestClient,
) -> None:
    login = await _browser_login(client)
    encoded_session = client.cookies.get(SESSION_COOKIE)
    assert encoded_session

    denied = await client.post("/session/logout", headers={"Origin": ORIGIN})
    accepted = await client.post(
        "/session/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": str(login["csrf_token"])},
    )

    assert denied.status_code == HTTP_403_FORBIDDEN
    assert accepted.status_code == HTTP_204_NO_CONTENT
    assert "Max-Age=0" in accepted.headers["set-cookie"]
    assert (await client.get("/session")).status_code == HTTP_401_UNAUTHORIZED

    # The server-side token-version bump, rather than cookie deletion alone,
    # invalidates a stolen copy of the session JWT.
    client.cookies.set(SESSION_COOKIE, encoded_session)
    assert (await client.get("/session")).status_code == HTTP_401_UNAUTHORIZED


async def test_browser_and_bearer_jwts_are_transport_isolated(
    client: AsyncTestClient,
) -> None:
    login = await _browser_login(client)
    browser_jwt = client.cookies.get(SESSION_COOKIE)
    bearer_jwt = await _bearer(client)
    assert browser_jwt

    browser_as_bearer = await client.get("/me", headers={"Authorization": f"Bearer {browser_jwt}"})
    client.cookies.set(SESSION_COOKIE, bearer_jwt)
    bearer_as_browser = await client.get("/session")

    assert browser_as_bearer.status_code == HTTP_401_UNAUTHORIZED
    assert bearer_as_browser.status_code == HTTP_401_UNAUTHORIZED
    assert login["csrf_token"]


async def test_cross_origin_mutation_is_rejected_even_with_valid_csrf(
    client: AsyncTestClient,
) -> None:
    login = await _browser_login(client)

    response = await client.post(
        "/organizations",
        json={"name": "Cross Site"},
        headers={
            "Origin": "https://attacker.example",
            "X-CSRF-Token": str(login["csrf_token"]),
        },
    )

    assert response.status_code == HTTP_403_FORBIDDEN


async def test_cookie_management_get_is_csrf_exempt(client: AsyncTestClient) -> None:
    await _browser_login(client)

    response = await client.get("/organizations")

    assert response.status_code == HTTP_200_OK


async def test_failed_browser_login_does_not_issue_a_cookie(client: AsyncTestClient) -> None:
    response = await client.post(
        "/session/login",
        json={"email": ADMIN_EMAIL, "password": "wrong-password"},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == HTTP_401_UNAUTHORIZED
    assert SESSION_COOKIE not in client.cookies
