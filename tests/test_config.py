"""Unit tests for Settings validation (production fail-fast)."""

from __future__ import annotations

import dataclasses

import pytest

from litestar_test.config import (
    DEFAULT_JWT_SECRET,
    InsecureConfigurationError,
    Settings,
)

STRONG_SECRET = "a-strong-random-production-secret-0123456789"

# A valid development baseline; tests derive variants via dataclasses.replace,
# which re-runs the same __post_init__ validation.
_BASE = Settings(
    database_url="sqlite+aiosqlite:///:memory:",
    admin_email="admin@example.com",
    master_key="master",
    jwt_secret=STRONG_SECRET,
    salt_key="salt",
    environment="development",
)


def test_production_rejects_default_jwt_secret() -> None:
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="production", jwt_secret=DEFAULT_JWT_SECRET)


def test_production_rejects_empty_jwt_secret() -> None:
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="prod", jwt_secret="")


def test_production_accepts_strong_jwt_secret() -> None:
    settings = dataclasses.replace(_BASE, environment="production", jwt_secret=STRONG_SECRET)
    assert settings.is_production is True


def test_development_allows_default_jwt_secret() -> None:
    # The dev default is intentionally permitted outside production.
    settings = dataclasses.replace(_BASE, environment="development", jwt_secret=DEFAULT_JWT_SECRET)
    assert settings.is_production is False


def test_sso_enabled_requires_discovery_client_id_and_secret() -> None:
    # All three are needed; missing any one keeps SSO off (routes unregistered)
    # rather than booting a broken or secret-less public-client flow.
    full = dataclasses.replace(
        _BASE,
        oidc_discovery_url="https://idp.example/.well-known/openid-configuration",
        oidc_client_id="client-abc",
        oidc_client_secret="shhh",  # pragma: allowlist secret
    )
    assert full.sso_enabled is True
    assert dataclasses.replace(full, oidc_client_secret=None).sso_enabled is False
    assert dataclasses.replace(full, oidc_client_id=None).sso_enabled is False
    assert dataclasses.replace(full, oidc_discovery_url=None).sso_enabled is False
