"""Plan 18 Phase 0 — `OPENAI_COMPATIBLE_ALLOWED_HOSTS` reaches `Settings`."""

from __future__ import annotations

import ipaddress

import pytest

from litestar_gateway.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "admin_email": "admin@example.com",
        "master_key": "x" * 24,
        "jwt_secret": "y" * 32,
        "salt_key": "z" * 32,
        "environment": "local",
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


def test_defaults_to_an_empty_allowlist() -> None:
    # The upgrade-safety property, asserted at the config layer.
    assert _settings().egress_allowlist().is_empty


def test_entries_are_parsed_from_the_comma_separated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_ALLOWED_HOSTS", " vllm.internal:8000 , 10.42.0.0/16 ")
    monkeypatch.setenv("ENVIRONMENT", "local")
    allowlist = Settings.from_env().egress_allowlist()
    assert allowlist.permits("vllm.internal", 8000, ())
    assert allowlist.permits("anything", 443, (ipaddress.ip_address("10.42.1.2"),))


def test_a_malformed_entry_fails_at_construction_even_locally() -> None:
    # Not gated behind the production checks: a typo should surface on the
    # developer's machine, not on the first call in staging.
    with pytest.raises(ValueError):
        _settings(openai_compatible_allowed_hosts=("vllm.internal:notaport",))
