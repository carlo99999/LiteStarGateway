"""Provider errors (429/5xx/timeout) must map to client status codes, not 500.

Unit tests cover the SDK-exception → domain-error translation; the integration
tests drive the HTTP endpoint with a monkeypatched SDK client raising each error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors
from litestar.status_codes import (
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_502_BAD_GATEWAY,
    HTTP_504_GATEWAY_TIMEOUT,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.exceptions import (
    ModelNotFound,
    UpstreamRateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from litestar_gateway.infrastructure.llm import openai_adapter
from litestar_gateway.infrastructure.llm.errors import translate_stream, translate_upstream_error

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
SALT_KEY = "unit-test-salt-key"


def _http_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return httpx.Response(status, headers=headers, request=request)


def test_openai_rate_limit_maps_to_429_with_retry_after() -> None:
    exc = openai.RateLimitError(
        "rate limited", response=_http_response(429, {"retry-after": "7"}), body=None
    )
    mapped = translate_upstream_error(exc)
    assert isinstance(mapped, UpstreamRateLimited)
    assert mapped.retry_after == "7"


def test_openai_5xx_maps_to_unavailable() -> None:
    exc = openai.APIStatusError("boom", response=_http_response(503), body=None)
    assert isinstance(translate_upstream_error(exc), UpstreamUnavailable)


def test_anthropic_overloaded_maps_to_unavailable() -> None:
    exc = anthropic.APIStatusError("overloaded", response=_http_response(529), body=None)
    assert isinstance(translate_upstream_error(exc), UpstreamUnavailable)


def test_genai_429_maps_to_rate_limited() -> None:
    exc = genai_errors.APIError(429, {"error": {"message": "quota exceeded"}})
    assert isinstance(translate_upstream_error(exc), UpstreamRateLimited)


def test_timeouts_map_to_upstream_timeout() -> None:
    assert isinstance(translate_upstream_error(httpx.ReadTimeout("slow")), UpstreamTimeout)
    # SDK timeout errors subclass their connection errors; the timeout mapping must win.
    sdk_timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://x"))
    assert isinstance(translate_upstream_error(sdk_timeout), UpstreamTimeout)


def test_connection_error_maps_to_unavailable() -> None:
    exc = httpx.ConnectError("refused")
    assert isinstance(translate_upstream_error(exc), UpstreamUnavailable)


def test_unrelated_errors_pass_through() -> None:
    assert translate_upstream_error(ValueError("bug")) is None
    assert translate_upstream_error(ModelNotFound("m")) is None


async def test_translate_stream_maps_midstream_errors() -> None:
    async def broken() -> AsyncIterator[dict]:
        yield {"ok": True}
        raise openai.APIStatusError("boom", response=_http_response(502), body=None)

    stream = translate_stream(broken())
    assert await anext(stream) == {"ok": True}
    with pytest.raises(UpstreamUnavailable):
        await anext(stream)


class _RaisingClient:
    """Async OpenAI-SDK stand-in whose chat.completions.create raises."""

    error: Exception = RuntimeError("unset")

    def __init__(self, **kwargs) -> None:
        self.chat = type("_Chat", (), {"completions": self})()

    async def close(self) -> None:
        return None

    async def create(self, **kwargs):
        raise _RaisingClient.error


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'up.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient) -> str:
    """Configure a credential + team + chat model 'm' + key. Returns the API key."""
    admin = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    cred = (
        await client.post(
            "/credentials",
            json={
                "name": "c-openai",
                "provider": "openai",
                "values": {"api_key": "sk-x"},  # pragma: allowlist secret
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
            "enabled": True,
        },
        headers=_bearer(admin),
    )
    return (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            openai.RateLimitError(
                "rate limited", response=_http_response(429, {"retry-after": "3"}), body=None
            ),
            HTTP_429_TOO_MANY_REQUESTS,
        ),
        (
            openai.InternalServerError("bad", response=_http_response(503), body=None),
            HTTP_502_BAD_GATEWAY,
        ),
        (
            openai.APITimeoutError(request=httpx.Request("POST", "https://x")),
            HTTP_504_GATEWAY_TIMEOUT,
        ),
    ],
)
async def test_provider_error_surfaces_as_client_status(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
) -> None:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", _RaisingClient)
    _RaisingClient.error = error
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == expected_status
    if expected_status == HTTP_429_TOO_MANY_REQUESTS:
        assert resp.headers.get("retry-after") == "3"
