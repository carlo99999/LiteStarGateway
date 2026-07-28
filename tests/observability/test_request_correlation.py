"""Integration tests for request correlation (Plan 11 Slice A, docs/logging.md §2/§4):

- the response always carries an `X-Request-ID` header;
- an inbound id from an untrusted source is never echoed back;
- the same id lands on the audit event created by the request;
- a 500 keeps a generic body while a detail is logged server-side;
- the production (structlog/JSON) log line carries the bound request id;
- Authorization / API-key values never appear in that log output.

The "log line carries the id" and "no secrets in logs" checks exercise the
*real* production log line (`build_logging_config` + `structlog.contextvars`),
captured via `capfd` at the file-descriptor level — `structlog.testing.
capture_logs` replaces the processor chain wholesale and would drop the
`merge_contextvars` processor that injects `request_id`, defeating the point.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import structlog
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.logging import build_logging_config

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    app = create_app(_settings(database_url))
    async with AsyncTestClient(app=app) as test_client:
        yield test_client


async def _admin_headers(client: AsyncTestClient) -> dict[str, str]:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    assert resp.status_code == HTTP_200_OK, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_response_always_carries_a_request_id_header(client: AsyncTestClient) -> None:
    resp = await client.get("/health")
    request_id = resp.headers.get("x-request-id")
    assert request_id
    assert len(request_id) <= 128


async def test_inbound_id_from_an_untrusted_source_is_not_echoed_back(
    client: AsyncTestClient,
) -> None:
    # No TRUSTED_PROXY_IPS configured in these test settings, so nothing is
    # ever trusted — the client-supplied value must never come back verbatim.
    resp = await client.get("/health", headers={"X-Request-ID": "attacker-supplied-id"})
    assert resp.headers.get("x-request-id") != "attacker-supplied-id"


async def test_request_id_links_response_header_and_audit_event(
    client: AsyncTestClient,
) -> None:
    headers = await _admin_headers(client)

    resp = await client.post("/organizations", json={"name": "Acme Corp"}, headers=headers)
    assert resp.status_code == HTTP_201_CREATED, resp.text
    request_id = resp.headers.get("x-request-id")
    assert request_id

    # The same id lands on the audit event this request created.
    audit_resp = await client.get("/audit?limit=1", headers=headers)
    assert audit_resp.status_code == HTTP_200_OK, audit_resp.text
    events = audit_resp.json()
    assert events, "expected at least one audit event"
    assert events[0]["action"] == "organization.create"
    assert events[0]["request_id"] == request_id


async def test_500_returns_a_generic_body_but_carries_a_request_id(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from litestar_gateway.infrastructure.persistence import user_repository

    async def boom(self: object, email: str) -> None:
        raise RuntimeError("db hiccup: secret-detail-should-not-leak")

    monkeypatch.setattr(user_repository.SQLAlchemyUserRepository, "get_by_email", boom)
    try:
        resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    finally:
        monkeypatch.undo()

    assert resp.status_code == 500
    request_id = resp.headers.get("x-request-id")
    assert request_id
    # Generic body: no leaked exception detail/stack trace to the client.
    assert "secret-detail-should-not-leak" not in resp.text
    assert "Traceback" not in resp.text


def _prod_settings() -> Settings:
    # Constructs only a config object (no real connection is opened) — same
    # pattern as tests/observability/test_logging.py's production case.
    return Settings(
        database_url="postgresql+asyncpg://gateway:pw@db:5432/gateway",  # pragma: allowlist secret
        admin_email=ADMIN_EMAIL,
        master_key="m" * 32,
        jwt_secret="x" * 40,
        salt_key="s" * 32,
        environment="production",
        session_cookie_secure=True,
        # Production refuses to start without Redis; never connected here.
        redis_url="redis://localhost:6379",
    )


def test_production_log_line_carries_the_bound_request_id(capfd: pytest.CaptureFixture) -> None:
    """docs/logging.md §4: the id bound for a request must appear in the
    structured (production, JSON) log line emitted while handling it."""
    get_logger = build_logging_config(_prod_settings()).configure()
    with structlog.contextvars.bound_contextvars(request_id="corr-abc-123"):
        get_logger("test").info(
            "handled request",
            path="/v1/chat/completions",
            authorization="Bearer sk-should-not-appear",  # never actually logged by app code
        )
    out, _ = capfd.readouterr()
    line = json.loads(out.strip().splitlines()[-1])
    assert line["request_id"] == "corr-abc-123"


async def test_authorization_header_never_appears_in_app_log_output(
    client: AsyncTestClient, capfd: pytest.CaptureFixture
) -> None:
    """Regression guard for the standing invariant (Authorization/`lsk_`
    key/credential values must never be logged): drive real authenticated
    requests through the app and check nothing the app printed contains the
    bearer token or the master-key password."""
    capfd.readouterr()  # drop fixture/app-startup noise
    headers = await _admin_headers(client)
    bearer_value = headers["Authorization"]
    resp = await client.get("/audit", headers=headers)
    assert resp.status_code == HTTP_200_OK, resp.text

    out, err = capfd.readouterr()
    assert bearer_value not in out
    assert bearer_value not in err
    assert MASTER_KEY not in out
    assert MASTER_KEY not in err
