"""The request body-size limit is explicit, configurable, and wired to the app."""

from __future__ import annotations

from litestar_gateway.app import create_app
from litestar_gateway.config import DEFAULT_MAX_BODY_SIZE, Settings


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "admin_email": "admin@example.com",
        "master_key": "master-secret",  # pragma: allowlist secret
        "jwt_secret": "test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        "salt_key": "test-salt-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_default_body_size_is_applied() -> None:
    app = create_app(_settings())
    assert app.request_max_body_size == DEFAULT_MAX_BODY_SIZE


def test_body_size_is_configurable() -> None:
    app = create_app(_settings(max_body_size=512))
    assert app.request_max_body_size == 512
