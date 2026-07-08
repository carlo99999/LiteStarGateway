"""Shared fixtures for the OpenAI-compatible inference endpoint tests.

The provider SDKs are monkeypatched (`_patch`), so no real provider call is
made. Every sibling test module imports what it needs from here (e.g.
`from conftest import FakeClient, _setup, _bearer`) — pytest auto-injects only
the `client` fixture; everything else is a plain helper/class.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.llm import (
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


class _FakeStream:
    """Async-iterable of chunk objects, like the OpenAI SDK's AsyncStream."""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield _Result(chunk)


def _stream_chunks(model: str | None) -> list[dict]:
    base = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "model": model}
    return [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": "Hi"}}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        # Final usage chunk (emitted when stream_options.include_usage is set).
        {**base, "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
    ]


class FakeClient:
    """Captures construction + call kwargs; echoes an OpenAI-shaped response."""

    last_init: dict = {}
    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeClient.last_init = kwargs
        self.chat = SimpleNamespace(completions=self)
        self.responses = SimpleNamespace(create=self._responses_create)
        self.embeddings = SimpleNamespace(create=self._embed)
        self.images = SimpleNamespace(generate=self._image)

    async def close(self) -> None:  # AsyncOpenAI.close is a coroutine
        return None

    async def _responses_create(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        # Responses-API usage shape: input/output tokens (not prompt/completion).
        usage = {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
        if kwargs.get("stream"):
            return _FakeStream(
                [
                    {"type": "response.created", "response": {"id": "r", "status": "in_progress"}},
                    {"type": "response.output_text.delta", "delta": "Hi"},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "r",
                            "status": "completed",
                            "output_text": "Hi",
                            "usage": usage,
                        },
                    },
                ]
            )
        return _Result(
            {"id": "r", "object": "response", "model": kwargs.get("model"), "usage": usage}
        )

    async def _embed(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        return _Result(
            {
                "object": "list",
                "model": kwargs.get("model"),
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        )

    async def _image(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        return _Result({"created": 0, "data": [{"url": "https://img/cat.png"}]})

    async def create(self, **kwargs):
        FakeClient.last_kwargs = kwargs
        if kwargs.get("stream"):
            return _FakeStream(_stream_chunks(kwargs.get("model")))
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

    async def close(self) -> None:  # AsyncAnthropic.close is a coroutine
        return None

    async def create(self, **kwargs):
        FakeAnthropic.last_kwargs = kwargs
        if kwargs.get("stream") and kwargs.get("tool_choice"):
            # Structured output streamed as a forced tool: the JSON arrives via
            # input_json_delta events (partial_json), not text_delta.
            return _FakeStream(
                [
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg-x",
                            "usage": {"input_tokens": 3, "output_tokens": 1},
                        },
                    },
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "tool_use", "name": "answer", "input": {}},
                    },
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "input_json_delta", "partial_json": '{"answer":'},
                    },
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "input_json_delta", "partial_json": " 42}"},
                    },
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use"},
                        "usage": {"output_tokens": 5},
                    },
                    {"type": "message_stop"},
                ]
            )
        if kwargs.get("tool_choice"):
            # Forced structured-output tool: return the JSON as a tool_use input.
            return _Result(
                {
                    "id": "msg-x",
                    "type": "message",
                    "role": "assistant",
                    "model": kwargs.get("model"),
                    "content": [
                        {
                            "type": "tool_use",
                            "name": kwargs["tool_choice"]["name"],
                            "input": {"answer": 42},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                }
            )
        if kwargs.get("stream"):
            # Real Anthropic streams report input tokens on message_start and
            # cumulative output tokens on message_delta (top-level `usage`).
            return _FakeStream(
                [
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg-x",
                            "usage": {"input_tokens": 3, "output_tokens": 1},
                        },
                    },
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": " there"},
                    },
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 5},
                    },
                    {"type": "message_stop"},
                ]
            )
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

    async def embed_content(self, **kwargs):
        FakeGenaiClient.last_kwargs = kwargs
        return _Result({"embeddings": [{"values": [0.4, 0.5, 0.6]}]})

    async def generate_images(self, **kwargs):
        FakeGenaiClient.last_kwargs = kwargs
        return _Result({"generated_images": [{"image": {"image_bytes": b"PNGBYTES"}}]})

    async def generate_content_stream(self, **kwargs):
        FakeGenaiClient.last_kwargs = kwargs
        # Real Gemini streams carry (cumulative) usage_metadata on the final chunk.
        return _FakeStream(
            [
                {"candidates": [{"content": {"role": "model", "parts": [{"text": "ci"}]}}]},
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "ao"}]},
                            "finish_reason": "STOP",
                        }
                    ],
                    "usage_metadata": {
                        "prompt_token_count": 4,
                        "candidates_token_count": 2,
                        "total_token_count": 6,
                    },
                },
            ]
        )


class FakeGenaiClient:
    last_init: dict = {}
    last_kwargs: dict = {}
    closed: bool = False

    def __init__(self, **kwargs) -> None:
        FakeGenaiClient.last_init = kwargs
        FakeGenaiClient.closed = False
        self.aio = SimpleNamespace(models=_FakeGeminiModels(), aclose=self._aclose)
        self.models = _FakeGeminiModels()

    def close(self) -> None:  # genai.Client.close is sync
        FakeGenaiClient.closed = True

    async def _aclose(self) -> None:  # the async surface closes via client.aio.aclose()
        FakeGenaiClient.closed = True


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(azure_adapter, "AsyncAzureOpenAI", FakeClient)
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeAnthropic)
    monkeypatch.setattr(vertex_adapter.genai, "Client", FakeGenaiClient)
    # The fakes capture call args on the class (not an instance) because the
    # adapter under test constructs the SDK client itself, so no test ever
    # holds a reference to instantiate against. Reset before every test so a
    # test that forgets to trigger the call under test fails on a KeyError
    # instead of silently reading a stale value left by the previous test.
    FakeClient.last_init = {}
    FakeClient.last_kwargs = {}
    FakeAnthropic.last_init = {}
    FakeAnthropic.last_kwargs = {}
    FakeGenaiClient.last_init = {}
    FakeGenaiClient.last_kwargs = {}
    FakeGenaiClient.closed = False


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


async def _setup_team(
    client: AsyncTestClient,
    provider: str = "openai",
    values: dict | None = None,
    provider_model_id: str = "gpt-4o",
    enabled: bool = True,
    model_type: str = "chat",
    params: dict | None = None,
    params_enforced: dict | None = None,
    max_output_tokens: int | None = None,
) -> tuple[str, str, str]:
    """Configure a credential + team + model 'm' + key.

    Returns (team API key, team id, admin token)."""
    admin = await _admin(client)
    cred = (
        await client.post(
            "/credentials",
            json={
                "name": f"c-{provider}",
                "provider": provider,
                "values": values if values is not None else OPENAI_VALUES,
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
    model_json: dict = {
        "name": "m",
        "provider": provider,
        "credential_id": cred,
        "type": model_type,
        "provider_model_id": provider_model_id,
        "enabled": enabled,
    }
    if params is not None:
        model_json["params"] = params
    if params_enforced is not None:
        model_json["params_enforced"] = params_enforced
    if max_output_tokens is not None:
        model_json["max_output_tokens"] = max_output_tokens
    await client.post(f"/teams/{team}/models", json=model_json, headers=_bearer(admin))
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def _setup(
    client: AsyncTestClient,
    provider: str = "openai",
    values: dict | None = None,
    provider_model_id: str = "gpt-4o",
    enabled: bool = True,
    model_type: str = "chat",
    params: dict | None = None,
    params_enforced: dict | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Configure a credential + team + model 'm' + key. Returns the team API key."""
    key, _, _ = await _setup_team(
        client,
        provider=provider,
        values=values,
        provider_model_id=provider_model_id,
        enabled=enabled,
        model_type=model_type,
        params=params,
        params_enforced=params_enforced,
        max_output_tokens=max_output_tokens,
    )
    return key


async def _team_usage(client: AsyncTestClient, team: str, admin: str) -> list[dict]:
    return (await client.get(f"/teams/{team}/usage", headers=_bearer(admin))).json()
