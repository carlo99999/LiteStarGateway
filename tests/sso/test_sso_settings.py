"""Tests for the DB-backed SSO settings singleton: CRUD, secret handling,
validation, and the hot-reload contract (DB config takes effect with no
process restart, and takes precedence over legacy env vars)."""

from __future__ import annotations

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
