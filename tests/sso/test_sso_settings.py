"""Tests for the DB-backed SSO settings singleton: CRUD, secret handling,
validation, and the hot-reload contract (DB config takes effect with no
process restart, and takes precedence over legacy env vars)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app

from .conftest import ADMIN_GROUP, _admin_token, _settings

VALID_PAYLOAD = {
    "enabled": True,
    # RFC 2606 .invalid — guaranteed never to resolve, so a real login attempt
    # fails at DNS instead of hanging or hitting a live host.
    "discovery_url": "https://sso-settings-test.invalid/.well-known/openid-configuration",
    "client_id": "client-123",
    "client_secret": "s3cr3t-value",  # pragma: allowlist secret
    "scopes": "openid email profile groups",
    "admin_groups": [ADMIN_GROUP],
    "default_admin": False,
    "team_mapping": {},
    "redirect_uri": None,
}


def _plain_client(tmp_path: Path) -> AsyncTestClient:
    """No `identity_provider` override — exercises the real DB/env resolution
    path (`explicit_override=False`) instead of the FakeIdP test seam other
    SSO tests use."""
    return AsyncTestClient(app=create_app(_settings(tmp_path)))


async def _admin_headers(client: AsyncTestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _admin_token(client)}"}


async def test_sso_routes_exist_even_with_nothing_configured(tmp_path: Path) -> None:
    """Routes register unconditionally now — enabling SSO later must not
    require a restart."""
    async with _plain_client(tmp_path) as client:
        resp = await client.get("/sso/login", follow_redirects=False)
        assert resp.status_code == HTTP_404_NOT_FOUND
        assert "is configured" in resp.json()["detail"].lower()


async def test_get_settings_before_any_upsert_is_404(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        resp = await client.get("/platform/sso-settings", headers=await _admin_headers(client))
        assert resp.status_code == HTTP_404_NOT_FOUND


async def test_settings_endpoints_require_platform_admin(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        resp = await client.get("/platform/sso-settings")
        assert resp.status_code == HTTP_401_UNAUTHORIZED
        resp = await client.put("/platform/sso-settings", json=VALID_PAYLOAD)
        assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_enabling_without_discovery_url_is_rejected(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        resp = await client.put("/platform/sso-settings", json={"enabled": True}, headers=headers)
        assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_enabling_without_ever_having_a_secret_is_rejected(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        payload = {**VALID_PAYLOAD, "client_secret": None}
        resp = await client.put("/platform/sso-settings", json=payload, headers=headers)
        assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_upsert_round_trip_never_exposes_the_secret(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        resp = await client.put("/platform/sso-settings", json=VALID_PAYLOAD, headers=headers)
        assert resp.status_code == HTTP_200_OK
        body = resp.json()
        assert "client_secret" not in body
        assert body["has_client_secret"] is True
        assert body["discovery_url"] == VALID_PAYLOAD["discovery_url"]

        resp = await client.get("/platform/sso-settings", headers=headers)
        assert resp.status_code == HTTP_200_OK
        assert "client_secret" not in resp.json()


async def test_put_without_client_secret_keeps_the_existing_one(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        await client.put("/platform/sso-settings", json=VALID_PAYLOAD, headers=headers)

        updated = {**VALID_PAYLOAD, "client_secret": None, "scopes": "openid email"}
        resp = await client.put("/platform/sso-settings", json=updated, headers=headers)
        assert resp.status_code == HTTP_200_OK
        body = resp.json()
        assert body["has_client_secret"] is True
        assert body["scopes"] == "openid email"


async def test_team_mapping_rejects_unknown_role(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        team_id = "00000000-0000-0000-0000-000000000001"
        payload = {
            **VALID_PAYLOAD,
            "enabled": False,
            "team_mapping": {"group-a": [{"team": team_id, "role": "owner"}]},
        }
        resp = await client.put("/platform/sso-settings", json=payload, headers=headers)
        assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_team_mapping_rejects_conflicting_roles_for_one_team(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        team_id = "00000000-0000-0000-0000-000000000001"
        payload = {
            **VALID_PAYLOAD,
            "enabled": False,
            "team_mapping": {
                "group-a": [{"team": team_id, "role": "member"}],
                "group-b": [{"team": team_id, "role": "billing-viewer"}],
            },
        }
        resp = await client.put("/platform/sso-settings", json=payload, headers=headers)
        assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_db_config_takes_precedence_and_is_actually_used(tmp_path: Path) -> None:
    """The strongest proof the DB path is live (not silently ignored): an
    unreachable-but-configured IdP fails at the discovery fetch (401,
    SSOExchangeError) rather than 404 (SSONotConfigured) — i.e. the DB row
    was read and its `OIDCIdentityProvider` was actually invoked."""
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        resp = await client.put("/platform/sso-settings", json=VALID_PAYLOAD, headers=headers)
        assert resp.status_code == HTTP_200_OK

        resp = await client.get("/sso/login", follow_redirects=False)
        assert resp.status_code == HTTP_401_UNAUTHORIZED
        assert "discovery" in resp.json()["detail"].lower()


async def test_disabling_falls_back_to_not_configured(tmp_path: Path) -> None:
    async with _plain_client(tmp_path) as client:
        headers = await _admin_headers(client)
        await client.put("/platform/sso-settings", json=VALID_PAYLOAD, headers=headers)
        disabled = {**VALID_PAYLOAD, "enabled": False}
        resp = await client.put("/platform/sso-settings", json=disabled, headers=headers)
        assert resp.status_code == HTTP_200_OK

        resp = await client.get("/sso/login", follow_redirects=False)
        assert resp.status_code == HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# ISSUE-028: outside local, the callback URL must be explicit here too.
# ---------------------------------------------------------------------------


def _prod_client(tmp_path: Path) -> AsyncTestClient:
    """Same app, deployed (staging) environment. The env-var path already
    refuses to enable SSO without `OIDC_REDIRECT_URI` outside local
    (`config.py`), because otherwise the callback is derived from the request's
    `Host`. Staging rather than production only because production
    additionally demands PostgreSQL; the SSO rule is the same for both."""
    return AsyncTestClient(app=create_app(_settings(tmp_path, environment="staging")))


async def test_enabling_without_a_redirect_uri_is_rejected_outside_local(
    tmp_path: Path,
) -> None:
    async with _prod_client(tmp_path) as client:
        resp = await client.put(
            "/platform/sso-settings",
            json={**VALID_PAYLOAD, "redirect_uri": None},
            headers=await _admin_headers(client),
        )
        assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text
        assert "redirect_uri" in resp.text


async def test_a_configured_redirect_uri_is_accepted_outside_local(tmp_path: Path) -> None:
    async with _prod_client(tmp_path) as client:
        resp = await client.put(
            "/platform/sso-settings",
            json={**VALID_PAYLOAD, "redirect_uri": "https://gw.example.com/sso/callback"},
            headers=await _admin_headers(client),
        )
        assert resp.status_code == HTTP_200_OK, resp.text
        assert resp.json()["redirect_uri"] == "https://gw.example.com/sso/callback"


async def test_disabling_sso_outside_local_needs_no_redirect_uri(tmp_path: Path) -> None:
    # The requirement belongs to an *enabled* configuration; turning SSO off
    # must never be blocked by it.
    async with _prod_client(tmp_path) as client:
        resp = await client.put(
            "/platform/sso-settings",
            json={**VALID_PAYLOAD, "enabled": False, "redirect_uri": None},
            headers=await _admin_headers(client),
        )
        assert resp.status_code == HTTP_200_OK, resp.text


async def test_local_development_still_derives_the_callback(tmp_path: Path) -> None:
    # Deriving from the request host is a genuine convenience on localhost;
    # only deployed environments lose it.
    async with _plain_client(tmp_path) as client:
        resp = await client.put(
            "/platform/sso-settings",
            json={**VALID_PAYLOAD, "redirect_uri": None},
            headers=await _admin_headers(client),
        )
        assert resp.status_code == HTTP_200_OK, resp.text


# ---------------------------------------------------------------------------
# ISSUE-032: a legacy row already in the database must not reopen the fallback.
# ---------------------------------------------------------------------------


async def _seed_legacy_enabled_row_without_redirect(tmp_path: Path) -> None:
    """Write directly what an upsert accepted before #398: enabled, with a
    client secret, and no callback URL. The write path refuses this now, so the
    only way to have one is to have created it earlier."""
    import sqlite3
    from uuid import uuid4

    connection = sqlite3.connect(tmp_path / "sso.db")
    try:
        now = datetime.now(UTC).isoformat()
        key_id = uuid4().bytes
        connection.execute(
            "INSERT INTO secret_key (id, purpose, material, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (key_id, "sso", "x", now, now),
        )
        connection.execute(
            "INSERT INTO sso_settings (id, enabled, discovery_url, client_id, "
            "encrypted_client_secret, key_id, scopes, admin_groups, default_admin, "
            "team_mapping, redirect_uri, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uuid4().bytes,
                1,
                "https://legacy-idp.invalid/.well-known/openid-configuration",
                "client-legacy",
                "ciphertext",
                key_id,
                "openid email profile",
                "[]",
                0,
                "{}",
                None,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()


async def test_a_legacy_enabled_row_without_redirect_is_refused_outside_local(
    tmp_path: Path,
) -> None:
    """#398 closed the write path; the resolver still loaded any enabled row,
    so a configuration created before the fix kept deriving the callback from
    the request `Host`. The runtime must refuse it, not use it."""
    async with _prod_client(tmp_path) as client:
        await _admin_headers(client)  # boots the schema and the admin user
    await _seed_legacy_enabled_row_without_redirect(tmp_path)

    async with _prod_client(tmp_path) as client:
        resp = await client.get(
            "/sso/login", follow_redirects=False, headers={"Host": "attacker.example"}
        )
        assert resp.status_code == HTTP_404_NOT_FOUND, resp.text
        assert "attacker.example" not in resp.text


async def test_a_legacy_row_without_redirect_still_works_in_local_development(
    tmp_path: Path,
) -> None:
    async with _plain_client(tmp_path) as client:
        await _admin_headers(client)
    await _seed_legacy_enabled_row_without_redirect(tmp_path)

    async with _plain_client(tmp_path) as client:
        resp = await client.get("/sso/login", follow_redirects=False)
        # Reaches the IdP (which does not resolve — RFC 2606 .invalid), i.e. the
        # configuration was accepted rather than refused.
        assert resp.status_code != HTTP_404_NOT_FOUND, resp.text
