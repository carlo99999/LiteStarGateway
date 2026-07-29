"""Allowed-hosts enforcement.

Several places derive a URL from the request's `Host` — the SSO callback when no
fixed one is configured being the one that produced ISSUE-028 and ISSUE-032. The
service-level rules closed those two instances; this closes the class, so the
next feature that reaches for `request.base_url` is not a new finding.

Mandatory outside local: a deployed gateway knows its own hostnames, and the
default of accepting any `Host` is only defensible on localhost.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import InsecureConfigurationError, Settings

POSTGRES_URL = "postgresql+asyncpg://gateway:pw@db:5432/gateway"  # pragma: allowlist secret
REDIS_URL = "redis://localhost:6379"


def _settings(tmp_path: Path, **overrides) -> Settings:
    values: dict = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'hosts.db'}",
        "admin_email": "admin@example.com",
        "master_key": "a-strong-random-master-key-0123456789",  # pragma: allowlist secret
        "jwt_secret": "a-strong-random-jwt-secret-0123456789",  # pragma: allowlist secret
        "salt_key": "a-strong-random-salt-key-0123456789",  # pragma: allowlist secret
        "environment": "staging",
        "session_cookie_secure": True,
        "redis_url": REDIS_URL,
        "allowed_hosts": ("gateway.example.com",),
    }
    values.update(overrides)
    return Settings(**values)


async def test_a_configured_host_is_served(tmp_path: Path) -> None:
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        resp = await client.get("/health", headers={"Host": "gateway.example.com"})
        assert resp.status_code == HTTP_200_OK


async def test_an_unexpected_host_is_refused(tmp_path: Path) -> None:
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as client:
        resp = await client.get("/health", headers={"Host": "attacker.example"})
        assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_a_wildcard_subdomain_entry_is_honoured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, allowed_hosts=("*.example.com",))
    async with AsyncTestClient(app=create_app(settings)) as client:
        assert (
            await client.get("/health", headers={"Host": "gw.example.com"})
        ).status_code == HTTP_200_OK
        assert (
            await client.get("/health", headers={"Host": "gw.attacker.example"})
        ).status_code == HTTP_400_BAD_REQUEST


def test_a_deployed_environment_must_declare_its_hosts(tmp_path: Path) -> None:
    with pytest.raises(InsecureConfigurationError, match="ALLOWED_HOSTS"):
        _settings(tmp_path, allowed_hosts=())


async def test_local_development_accepts_any_host(tmp_path: Path) -> None:
    # localhost, 127.0.0.1, a container name, a tunnel hostname — requiring the
    # list here would buy nothing and break every ad-hoc setup.
    settings = _settings(
        tmp_path, environment="development", session_cookie_secure=False, allowed_hosts=()
    )
    async with AsyncTestClient(app=create_app(settings)) as client:
        resp = await client.get("/health", headers={"Host": "anything.local"})
        assert resp.status_code == HTTP_200_OK


def test_a_bare_wildcard_is_refused(tmp_path: Path) -> None:
    """`ALLOWED_HOSTS=*` is the obvious thing to write to get past a new
    mandatory setting, and it silently restored the exact behaviour the setting
    exists to remove: the middleware short-circuits on a `*` entry and accepts
    every `Host`. Non-emptiness was the only check, so it passed."""
    with pytest.raises(InsecureConfigurationError, match=r"\*"):
        _settings(tmp_path, allowed_hosts=("*",))


def test_a_wildcard_beside_real_hosts_is_refused_too(tmp_path: Path) -> None:
    # One `*` anywhere in the list disables the check for the whole list, so a
    # config that looks specific is not.
    with pytest.raises(InsecureConfigurationError, match=r"\*"):
        _settings(tmp_path, allowed_hosts=("gateway.example.com", "*"))


def test_a_bare_wildcard_is_still_allowed_locally(tmp_path: Path) -> None:
    # Local already accepts any Host with an empty list; refusing the explicit
    # spelling of the same thing would be noise.
    _settings(
        tmp_path, environment="development", session_cookie_secure=False, allowed_hosts=("*",)
    )


def test_a_subdomain_wildcard_is_not_the_bare_one(tmp_path: Path) -> None:
    # `*.example.com` is a real constraint and must keep working; only the
    # matches-everything spelling is refused.
    _settings(tmp_path, allowed_hosts=("*.example.com",))
