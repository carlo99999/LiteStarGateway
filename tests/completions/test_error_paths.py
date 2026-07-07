"""Clean error responses for misconfiguration (never a leaking 500)."""

from __future__ import annotations

import pytest
from litestar.status_codes import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_501_NOT_IMPLEMENTED,
)
from litestar.testing import AsyncTestClient

from .conftest import _bearer, _patch, _setup


async def test_vertex_invalid_service_account_json_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A malformed vertex_credentials JSON must yield a clean 400, never a 500 that
    # could echo private-key material.
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="vertex_ai",
        values={"vertex_project": "p", "vertex_location": "us", "vertex_credentials": "{not-json"},
        provider_model_id="gemini-1.5-pro",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_missing_api_key_in_credential_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A credential without api_key yields a clean 400 at call time, not a 500.
    # Creation-time validation would reject it now, so bypass it to simulate a
    # legacy row created before validation existed (defense in depth).
    from litestar_gateway.application import credential_service

    monkeypatch.setattr(credential_service, "validate_credential_values", lambda *_: None)
    _patch(monkeypatch)
    api_key = await _setup(client, values={})  # credential with no api_key
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_unknown_model_alias_404(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_disabled_model_409(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client, enabled=False)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_409_CONFLICT


async def test_unsupported_provider_501(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    # 'bedrock' has no adapter in the gateway yet → 501.
    api_key = await _setup(client, provider="bedrock")
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
