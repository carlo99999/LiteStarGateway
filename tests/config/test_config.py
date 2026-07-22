"""Unit tests for Settings validation (production fail-fast)."""

from __future__ import annotations

import dataclasses
import json
from uuid import UUID

import pytest

from litestar_gateway.config import (
    DEFAULT_JWT_SECRET,
    SAMPLE_MASTER_KEY,
    InsecureConfigurationError,
    Settings,
    TeamGrant,
    _env_team_mapping,
)
from litestar_gateway.domain.entities import TeamRole

STRONG_SECRET = "a-strong-random-production-secret-0123456789"
POSTGRES_URL = "postgresql+asyncpg://gateway:pw@db:5432/gateway"  # pragma: allowlist secret

# A valid development baseline; tests derive variants via dataclasses.replace,
# which re-runs the same __post_init__ validation.
_BASE = Settings(
    database_url="sqlite+aiosqlite:///:memory:",
    admin_email="admin@example.com",
    master_key="a-strong-random-master-key-0123456789",
    jwt_secret=STRONG_SECRET,
    salt_key="a-strong-random-salt-key-0123456789",
    environment="development",
    session_cookie_secure=True,
)


def test_production_rejects_default_jwt_secret() -> None:
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="production", jwt_secret=DEFAULT_JWT_SECRET)


def test_production_rejects_empty_jwt_secret() -> None:
    with pytest.raises(InsecureConfigurationError):
        dataclasses.replace(_BASE, environment="prod", jwt_secret="")


def test_production_accepts_strong_jwt_secret() -> None:
    settings = dataclasses.replace(
        _BASE, environment="production", jwt_secret=STRONG_SECRET, database_url=POSTGRES_URL
    )
    assert settings.is_production is True


def test_production_rejects_sqlite_database_url() -> None:
    # The container image no longer defaults DATABASE_URL to SQLite: a "prod"
    # deployment that forgot to point at Postgres must fail at startup, not
    # boot single-writer per-container storage that diverges across replicas.
    with pytest.raises(InsecureConfigurationError, match="DATABASE_URL"):
        dataclasses.replace(_BASE, environment="production")


def test_production_accepts_postgres_database_url() -> None:
    settings = dataclasses.replace(_BASE, environment="production", database_url=POSTGRES_URL)
    assert settings.is_postgres is True


def test_staging_still_allows_sqlite() -> None:
    # The Postgres requirement is scoped to production/prod; other non-local
    # environments keep SQLite available (secrets are still validated there).
    settings = dataclasses.replace(_BASE, environment="staging")
    assert settings.is_postgres is False


def test_non_local_env_rejects_insecure_session_cookies() -> None:
    with pytest.raises(InsecureConfigurationError, match="SESSION_COOKIE_SECURE"):
        dataclasses.replace(_BASE, environment="staging", session_cookie_secure=False)


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
        _BASE,
        environment="production",
        jwt_secret=STRONG_SECRET,
        salt_key=None,
        database_url=POSTGRES_URL,
    )
    assert settings.is_production is True


_SSO = {
    "oidc_discovery_url": "https://idp.example/.well-known/openid-configuration",
    "oidc_client_id": "client-abc",
    "oidc_client_secret": "s3cr3t-value",  # pragma: allowlist secret
}


def test_sso_outside_local_requires_redirect_uri() -> None:
    # M31: without OIDC_REDIRECT_URI the callback URL is derived from the
    # untrusted Host header — reject it outside local dev.
    with pytest.raises(InsecureConfigurationError, match="OIDC_REDIRECT_URI"):
        dataclasses.replace(_BASE, environment="staging", oidc_redirect_uri=None, **_SSO)


def test_sso_outside_local_accepts_explicit_redirect_uri() -> None:
    settings = dataclasses.replace(
        _BASE,
        environment="staging",
        oidc_redirect_uri="https://gateway.example.com/sso/callback",
        **_SSO,
    )
    assert settings.sso_enabled is True


def test_sso_in_local_allows_derived_redirect_uri() -> None:
    # Local dev may derive the callback from the request; the check is skipped.
    settings = dataclasses.replace(_BASE, environment="development", oidc_redirect_uri=None, **_SSO)
    assert settings.sso_enabled is True


def test_from_env_rejects_non_numeric_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "ten")
    with pytest.raises(InsecureConfigurationError):
        Settings.from_env()


def test_from_env_rejects_negative_pool_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "-1")
    with pytest.raises(InsecureConfigurationError):
        Settings.from_env()


def test_from_env_zero_metrics_interval_disables_the_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0 is the documented "off" switch (app.py gates the publisher on a truthy
    # interval) — it must parse, not be rejected as below-minimum.
    monkeypatch.setenv("MLFLOW_METRICS_INTERVAL", "0")
    assert Settings.from_env().mlflow_metrics_interval == 0


def test_from_env_rejects_negative_metrics_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # The only numeric env var that had no validation coverage (issues/round-3.md L18).
    monkeypatch.setenv("MLFLOW_METRICS_INTERVAL", "-5")
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
    settings = dataclasses.replace(
        _BASE, environment="production", master_key=None, database_url=POSTGRES_URL
    )
    assert settings.is_production is True


def test_development_allows_weak_master_key() -> None:
    settings = dataclasses.replace(_BASE, environment="development", master_key="master")
    assert settings.is_production is False


def test_default_role_absent_defaults_to_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_ROLE", raising=False)
    settings = Settings.from_env()
    assert settings.default_role == "member"
    assert settings.default_admin is False


def test_default_role_admin_sets_default_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_ROLE", "admin")
    assert Settings.from_env().default_admin is True


def test_default_role_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_ROLE", "ADMIN")
    assert Settings.from_env().default_admin is True


def test_default_role_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_ROLE", "superuser")
    with pytest.raises(InsecureConfigurationError):
        Settings.from_env()


_TEAM_ID = "11111111-1111-1111-1111-111111111111"


def test_team_mapping_absent_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSO_TEAM_MAPPING", raising=False)
    assert _env_team_mapping("SSO_TEAM_MAPPING") == {}


def test_team_mapping_parses_groups_and_defaults_role_to_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SSO_TEAM_MAPPING",
        json.dumps({"eng": [{"team": _TEAM_ID, "role": "admin"}], "qa": [{"team": _TEAM_ID}]}),
    )
    mapping = _env_team_mapping("SSO_TEAM_MAPPING")
    assert mapping["eng"] == (TeamGrant(UUID(_TEAM_ID), TeamRole.ADMIN),)
    assert mapping["qa"] == (TeamGrant(UUID(_TEAM_ID), TeamRole.MEMBER),)


def test_team_mapping_rejects_conflicting_non_admin_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two groups granting the same team two different non-admin roles would make
    # the resolved role depend on the IdP's group ordering; fail at load instead.
    monkeypatch.setenv(
        "SSO_TEAM_MAPPING",
        json.dumps(
            {
                "eng": [{"team": _TEAM_ID, "role": "member"}],
                "ops": [{"team": _TEAM_ID, "role": "model-manager"}],
            }
        ),
    )
    with pytest.raises(InsecureConfigurationError, match="conflicting roles"):
        _env_team_mapping("SSO_TEAM_MAPPING")


def test_team_mapping_allows_same_team_same_role_in_two_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SSO_TEAM_MAPPING",
        json.dumps({"eng": [{"team": _TEAM_ID}], "qa": [{"team": _TEAM_ID, "role": "member"}]}),
    )
    mapping = _env_team_mapping("SSO_TEAM_MAPPING")
    assert mapping["eng"] == mapping["qa"] == (TeamGrant(UUID(_TEAM_ID), TeamRole.MEMBER),)


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",  # malformed JSON
        json.dumps(["eng"]),  # not an object
        json.dumps({"eng": {"team": _TEAM_ID}}),  # grants must be a list
        json.dumps({"eng": [{"team": "not-a-uuid"}]}),  # bad UUID
        json.dumps({"eng": [{"role": "admin"}]}),  # missing 'team'
        json.dumps({"eng": [{"team": _TEAM_ID, "role": "owner"}]}),  # unknown role
    ],
)
def test_team_mapping_rejects_malformed_input(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SSO_TEAM_MAPPING", raw)
    with pytest.raises(InsecureConfigurationError):
        _env_team_mapping("SSO_TEAM_MAPPING")


def test_dev_auto_creates_schema_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("AUTO_CREATE_SCHEMA", raising=False)
    assert Settings.from_env().auto_create_schema is True


def test_dev_container_can_disable_auto_create(monkeypatch: pytest.MonkeyPatch) -> None:
    # The migration-managed dev container sets this off so create_all and
    # `database upgrade` don't both try to create the same new tables.
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTO_CREATE_SCHEMA", "false")
    assert Settings.from_env().auto_create_schema is False


def test_production_disables_auto_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("SALT_KEY", "y" * 40)
    monkeypatch.setenv("MASTER_KEY", "z" * 40)
    monkeypatch.delenv("AUTO_CREATE_SCHEMA", raising=False)
    assert Settings.from_env().auto_create_schema is False
