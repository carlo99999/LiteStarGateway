"""Integration tests for the OpenAI-compatible inference endpoints.

The provider SDKs are monkeypatched, so no real provider call is made.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_501_NOT_IMPLEMENTED,
)
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings
from litestar_test.infrastructure.llm import (
    anthropic_adapter,
    azure_adapter,
    openai_adapter,
    vertex_adapter,
)

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"

OPENAI_VALUES = {"api_key": "sk-x"}
AZURE_VALUES = {
    "api_key": "az-key",
    "api_base": "https://acme.openai.azure.com",
    "api_version": "2024-02-15-preview",
}
DATABRICKS_VALUES = {"api_key": "dapi-x", "api_base": "https://w.databricks.com/serving-endpoints"}
ANTHROPIC_VALUES = {"api_key": "sk-ant-x"}
VERTEX_VALUES = {"vertex_project": "p", "vertex_location": "us-central1"}


class _Result:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


class FakeClient:
    """Captures construction + call kwargs; echoes an OpenAI-shaped response."""

    last_init: dict = {}
    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeClient.last_init = kwargs
        self.chat = SimpleNamespace(completions=self)
        self.responses = self

    async def create(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        return _Result(
            {
                "id": "cmpl-x",
                "object": "chat.completion",
                "created": 123,
                "model": kwargs.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


class FakeAnthropic:
    """Captures Messages-API kwargs; echoes an Anthropic message."""

    last_init: dict = {}
    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeAnthropic.last_init = kwargs
        self.messages = self

    async def create(self, **kwargs):
        FakeAnthropic.last_kwargs = kwargs
        return _Result(
            {
                "id": "msg-x",
                "type": "message",
                "role": "assistant",
                "model": kwargs.get("model"),
                "content": [{"type": "text", "text": "hi there"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 5},
            }
        )


class _FakeGeminiModels:
    async def generate_content(self, **kwargs):
        FakeGenaiClient.last_kwargs = kwargs
        return _Result(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "ciao"}]},
                        "finish_reason": "STOP",
                    }
                ],
                "usage_metadata": {
                    "prompt_token_count": 4,
                    "candidates_token_count": 2,
                    "total_token_count": 6,
                },
                "model_version": "gemini-1.5-pro-002",
                "response_id": "resp-g",
            }
        )


class FakeGenaiClient:
    last_init: dict = {}
    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeGenaiClient.last_init = kwargs
        self.aio = SimpleNamespace(models=_FakeGeminiModels())
        self.models = _FakeGeminiModels()


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(azure_adapter, "AsyncAzureOpenAI", FakeClient)
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeAnthropic)
    monkeypatch.setattr(vertex_adapter.genai, "Client", FakeGenaiClient)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'inf.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _setup(
    client: AsyncTestClient,
    provider: str = "openai",
    values: dict | None = None,
    provider_model_id: str = "gpt-4o",
    enabled: bool = True,
) -> str:
    """Configure a credential + team + model 'm' + key. Returns the team API key."""
    admin = await _admin(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": f"c-{provider}", "provider": provider, "values": values or OPENAI_VALUES},
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
            "provider": provider,
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": provider_model_id,
            "enabled": enabled,
        },
        headers=_bearer(admin),
    )
    return (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]


async def test_openai_chat_completions(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["model"] == "gpt-4o"  # alias -> upstream id
    assert FakeClient.last_init["api_key"] == "sk-x"
    assert resp.json()["model"] == "gpt-4o"


async def test_openai_responses(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK


async def test_azure_chat_and_responses(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="azure_openai", values=AZURE_VALUES, provider_model_id="my-deploy"
    )
    chat = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert chat.status_code == HTTP_200_OK
    # Azure client built with the endpoint + version from the credential.
    assert FakeClient.last_init["azure_endpoint"] == AZURE_VALUES["api_base"]
    assert FakeClient.last_init["api_version"] == AZURE_VALUES["api_version"]
    assert FakeClient.last_kwargs["model"] == "my-deploy"  # deployment name

    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK


async def test_databricks_chat_works(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="databricks", values=DATABRICKS_VALUES, provider_model_id="my-endpoint"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_init["base_url"] == DATABRICKS_VALUES["api_base"]


async def test_databricks_responses_emulated_over_chat(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Databricks has no native Responses API → emulated via chat.completions.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="databricks", values=DATABRICKS_VALUES, provider_model_id="my-endpoint"
    )
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "instructions": "be brief", "input": "hi"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["object"] == "response"
    assert body["output_text"] == "hello"
    assert body["model"] == "my-endpoint"
    # The Responses input was translated into chat messages (system + user).
    assert FakeClient.last_kwargs["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]


async def test_anthropic_chat_translation(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    # Request: system extracted, only user/assistant in messages, max_tokens defaulted.
    assert FakeAnthropic.last_kwargs["system"] == "be brief"
    assert FakeAnthropic.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert FakeAnthropic.last_kwargs["model"] == "claude-3-5-sonnet"
    assert FakeAnthropic.last_kwargs["max_tokens"] == 1024
    # Response: translated back to OpenAI chat shape.
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 3
    assert body["usage"]["completion_tokens"] == 5


async def test_anthropic_responses_emulated(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["object"] == "response"
    assert body["output_text"] == "hi there"


async def test_vertex_chat_translation(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="gemini-1.5-pro",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    # Client built for Vertex with project/location from the credential.
    assert FakeGenaiClient.last_init["vertexai"] is True
    assert FakeGenaiClient.last_init["project"] == "p"
    assert FakeGenaiClient.last_init["location"] == "us-central1"
    # Request: system -> system_instruction, assistant role would map to "model".
    assert FakeGenaiClient.last_kwargs["model"] == "gemini-1.5-pro"
    assert FakeGenaiClient.last_kwargs["config"]["system_instruction"] == "be brief"
    assert FakeGenaiClient.last_kwargs["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    # Response: translated back to OpenAI chat shape.
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "ciao"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 4
    assert body["usage"]["completion_tokens"] == 2


async def test_vertex_responses_emulated(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK
    assert resp.json()["output_text"] == "ciao"


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
