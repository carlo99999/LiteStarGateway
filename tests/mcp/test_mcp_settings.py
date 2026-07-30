"""Plan 20 S0 — `MCP_ALLOWED_HOSTS` reaches `Settings`.

The platform's one veto over a team-owned resource (design §2): team admins
register MCP servers freely, but only inside the hosts an operator authorized.
Same grammar and the same parser as the openai_compatible allowlist, so a
malformed entry fails at construction rather than being silently dropped.
"""

from __future__ import annotations

import pytest

from litestar_gateway.config import InsecureConfigurationError, Settings


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
    # Upgrade safety: a deployment that upgrades gains no new egress reach until
    # an operator opts in, so no team can register a server yet.
    assert _settings().mcp_allowlist().is_empty


def test_entries_are_parsed_like_the_provider_allowlist() -> None:
    allowlist = _settings(mcp_allowed_hosts=("tools.internal:8443", "10.9.0.0/16")).mcp_allowlist()

    assert not allowlist.is_empty
    assert allowlist.permits("tools.internal", 8443, ())
    # Wrong port on a name entry is a different target.
    assert not allowlist.permits("tools.internal", 9000, ())


def test_a_malformed_entry_fails_at_construction() -> None:
    # Even locally: a typo here would leave an operator believing a host is
    # authorized when it is not, which is worse than a startup failure.
    with pytest.raises((ValueError, InsecureConfigurationError)):
        _settings(mcp_allowed_hosts=("10.9.0.0/99",))


def test_the_two_allowlists_are_independent() -> None:
    # A host authorized for a self-hosted *model* is not thereby authorized as a
    # *tool* server: different data leaves the gateway for each.
    settings = _settings(
        openai_compatible_allowed_hosts=("vllm.internal:8000",),
        mcp_allowed_hosts=("tools.internal:8443",),
    )

    assert settings.egress_allowlist().permits("vllm.internal", 8000, ())
    assert not settings.egress_allowlist().permits("tools.internal", 8443, ())
    assert not settings.mcp_allowlist().permits("vllm.internal", 8000, ())
