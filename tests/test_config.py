"""Unit tests for Settings validation (production fail-fast)."""

from __future__ import annotations

import dataclasses

import pytest

from litestar_test.config import (
    DEFAULT_JWT_SECRET,
    SAMPLE_MASTER_KEY,
    InsecureConfigurationError,
    Settings,
)

STRONG_SECRET = "a-strong-random-production-secret-0123456789"

# A valid development baseline; tests derive variants via dataclasses.replace,
# which re-runs the same __post_init__ validation.
_BASE = Settings(
    database_url="sqlite+aiosqlite:///:memory:",
    admin_email="admin@example.com",
    master_key="a-strong-random-master-key-0123456789",
    jwt_secret=STRONG_SECRET,
    salt_key="a-strong-random-salt-key-0123456789",
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
    # The dev default is intentionally permitted in local environments.
    settings = dataclasses.replace(_BASE, environment="development", jwt_secret=DEFAULT_JWT_SECRET)
    assert settings.is_production is False


def test_non_local_env_rejects_default_jwt_secret() -> None:
    # Not just "production"/"prod": staging (or a typo) must also fail fast.
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="staging", jwt_secret=DEFAULT_JWT_SECRET)


def test_non_local_env_rejects_short_jwt_secret() -> None:
    weak = "too-short"
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="production", jwt_secret=weak)


def test_non_local_env_rejects_short_salt_key() -> None:
    weak = "short"
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(
            _BASE, environment="production", jwt_secret=STRONG_SECRET, salt_key=weak
        )


def test_production_allows_no_salt_key() -> None:
    # SALT_KEY is optional (credential encryption is opt-in).
    settings = dataclasses.replace(
        _BASE, environment="production", jwt_secret=STRONG_SECRET, salt_key=None
    )
    assert settings.is_production is True


def test_from_env_rejects_non_numeric_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "ten")
    with pytest.raises(InsecureConfigurationError):
        Settings.from_env()


def test_from_env_rejects_negative_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "-1")
    with pytest.raises(InsecureConfigurationError):
        Settings.from_env()


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


def test_non_local_env_rejects_sample_master_key() -> None:
    # .env.sample ships MASTER_KEY=change-me-please; a forgotten override would
    # create the platform admin with a publicly-known password.
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="production", master_key=SAMPLE_MASTER_KEY)


def test_non_local_env_rejects_short_master_key() -> None:
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="production", master_key="short-password")


def test_production_allows_no_master_key() -> None:
    # MASTER_KEY is only needed to bootstrap an empty users table.
    settings = dataclasses.replace(_BASE, environment="production", master_key=None)
    assert settings.is_production is True


def test_development_allows_weak_master_key() -> None:
    settings = dataclasses.replace(_BASE, environment="development", master_key="master")
    assert settings.is_production is False
