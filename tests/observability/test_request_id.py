"""Unit tests for request-id resolution: trusted-proxy acceptance and
length/charset validation (docs/logging.md §2, §4)."""

from __future__ import annotations

import structlog

from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.web.request_id import resolve_request_id
from litestar_gateway.request_context import current_request_id


def _settings(*, trusted_proxy_ips: tuple[str, ...] = ()) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        admin_email="admin@example.com",
        master_key=None,
        jwt_secret="x" * 40,
        salt_key=None,
        trusted_proxy_ips=trusted_proxy_ips,
    )


def test_no_inbound_header_generates_a_fresh_id() -> None:
    request_id = resolve_request_id(None, "10.0.0.1", _settings(trusted_proxy_ips=("10.0.0.1",)))
    assert request_id


def test_inbound_id_from_untrusted_source_is_replaced() -> None:
    settings = _settings(trusted_proxy_ips=("10.0.0.1",))
    resolved = resolve_request_id("client-supplied-id", "203.0.113.5", settings)
    assert resolved != "client-supplied-id"


def test_inbound_id_from_trusted_proxy_is_accepted_verbatim() -> None:
    settings = _settings(trusted_proxy_ips=("10.0.0.1",))
    resolved = resolve_request_id("client-supplied-id", "10.0.0.1", settings)
    assert resolved == "client-supplied-id"


def test_trusted_proxy_cidr_matches_a_covered_address() -> None:
    settings = _settings(trusted_proxy_ips=("10.0.0.0/24",))
    resolved = resolve_request_id("a-valid-id", "10.0.0.42", settings)
    assert resolved == "a-valid-id"


def test_malformed_value_is_replaced_even_from_a_trusted_proxy() -> None:
    """Length/charset validation applies regardless of trust — a trusted proxy
    forwarding a malformed value must not inject it into structured logs."""
    settings = _settings(trusted_proxy_ips=("10.0.0.1",))
    resolved = resolve_request_id("bad id; with spaces\n", "10.0.0.1", settings)
    assert resolved != "bad id; with spaces\n"


def test_overlong_value_is_replaced_even_from_a_trusted_proxy() -> None:
    settings = _settings(trusted_proxy_ips=("10.0.0.1",))
    overlong = "a" * 200
    resolved = resolve_request_id(overlong, "10.0.0.1", settings)
    assert resolved != overlong


def test_no_trusted_proxies_configured_never_trusts_inbound_value() -> None:
    settings = _settings(trusted_proxy_ips=())
    resolved = resolve_request_id("client-supplied-id", "10.0.0.1", settings)
    assert resolved != "client-supplied-id"


def test_current_request_id_is_none_outside_a_bound_context() -> None:
    assert current_request_id() is None


def test_current_request_id_reads_the_bound_contextvar() -> None:
    with structlog.contextvars.bound_contextvars(request_id="abc123"):
        assert current_request_id() == "abc123"
    assert current_request_id() is None
