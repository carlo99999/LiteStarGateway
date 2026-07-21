"""GET /v1/models — the OpenAI-compatible catalog — and the static security
headers on responses (minor hardening PR)."""

from __future__ import annotations

from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from .conftest import _bearer, _setup


async def test_models_list_returns_enabled_model_in_openai_shape(client: AsyncTestClient) -> None:
    key = await _setup(client)  # enabled chat model "m"
    resp = await client.get("/v1/models", headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["object"] == "list"
    entry = next(e for e in body["data"] if e["id"] == "m")
    assert entry["object"] == "model"
    assert isinstance(entry["created"], int)
    assert entry["owned_by"]


async def test_models_list_excludes_disabled_models(client: AsyncTestClient) -> None:
    key = await _setup(client, enabled=False)
    body = (await client.get("/v1/models", headers=_bearer(key))).json()
    assert all(e["id"] != "m" for e in body["data"])


async def test_models_list_requires_a_key(client: AsyncTestClient) -> None:
    assert (await client.get("/v1/models")).status_code == HTTP_401_UNAUTHORIZED


async def test_responses_carry_security_headers(client: AsyncTestClient) -> None:
    key = await _setup(client)
    resp = await client.get("/v1/models", headers=_bearer(key))
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # HSTS is TLS-only; the test app runs with session_cookie_secure=False.
    assert "strict-transport-security" not in resp.headers
