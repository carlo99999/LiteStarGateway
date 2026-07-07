"""`/v1/embeddings` and `/v1/images/generations`, per provider + type checks."""

from __future__ import annotations

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_501_NOT_IMPLEMENTED
from litestar.testing import AsyncTestClient

from .conftest import (
    ANTHROPIC_VALUES,
    DATABRICKS_VALUES,
    VERTEX_VALUES,
    FakeClient,
    FakeGenaiClient,
    _bearer,
    _patch,
    _setup,
    _setup_team,
    _team_usage,
)


async def test_embeddings_openai(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider_model_id="text-embedding-3-small", model_type="embeddings"
    )
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "m", "input": "hello"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["model"] == "text-embedding-3-small"
    assert FakeClient.last_kwargs["input"] == "hello"
    body = resp.json()
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]


async def test_embeddings_vertex(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="text-embedding-004",
        model_type="embeddings",
    )
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "m", "input": "hello"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeGenaiClient.last_kwargs["model"] == "text-embedding-004"
    assert FakeGenaiClient.last_kwargs["contents"] == "hello"
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"][0]["embedding"] == [0.4, 0.5, 0.6]


async def test_embeddings_vertex_bills_nonzero_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H14: Vertex embeddings used to record a UsageEvent with 0 tokens / 0 cost
    # (usage hardcoded to None). The meter now estimates from the input when the
    # provider reports no usage, so the spend is no longer invisible.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="text-embedding-004",
        model_type="embeddings",
    )
    resp = await client.post(
        "/v1/embeddings", json={"model": "m", "input": "hello"}, headers=_bearer(key)
    )
    assert resp.status_code == HTTP_200_OK
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 2  # ceil(len("hello")/4), not billed as zero


async def test_embeddings_wrong_model_type_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A chat model used on /v1/embeddings → type mismatch.
    _patch(monkeypatch)
    api_key = await _setup(client, model_type="chat")
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "m", "input": "hello"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_embeddings_unsupported_provider_501(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anthropic has no embeddings API → 501 (model type is correct).
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="irrelevant",
        model_type="embeddings",
    )
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "m", "input": "hello"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED


async def test_images_openai(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client, provider_model_id="dall-e-3", model_type="image")
    resp = await client.post(
        "/v1/images/generations",
        json={"model": "m", "prompt": "a cat", "size": "1024x1024"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["model"] == "dall-e-3"
    assert FakeClient.last_kwargs["prompt"] == "a cat"
    assert resp.json()["data"][0]["url"] == "https://img/cat.png"


async def test_images_vertex(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="imagen-3.0",
        model_type="image",
    )
    resp = await client.post(
        "/v1/images/generations",
        json={"model": "m", "prompt": "a cat"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeGenaiClient.last_kwargs["model"] == "imagen-3.0"
    assert FakeGenaiClient.last_kwargs["prompt"] == "a cat"
    # Imagen bytes are base64-encoded into OpenAI's b64_json field.
    assert resp.json()["data"][0]["b64_json"] == base64.b64encode(b"PNGBYTES").decode("ascii")


async def test_images_wrong_model_type_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client, model_type="chat")
    resp = await client.post(
        "/v1/images/generations",
        json={"model": "m", "prompt": "a cat"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_images_unsupported_provider_501(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Databricks has no image generation → 501 (model type is correct).
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="x",
        model_type="image",
    )
    resp = await client.post(
        "/v1/images/generations",
        json={"model": "m", "prompt": "a cat"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
