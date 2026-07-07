"""Smart routing through the chat endpoint: routing, filters, §4 fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

import litestar_gateway.application.routing.service as routing_service
from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


class EchoClient:
    """Fake AsyncOpenAI: echoes the requested model back in the response."""

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=self)

    async def close(self) -> None:
        return None

    async def create(self, **kwargs):
        data = {
            "id": "cmpl-x",
            "object": "chat.completion",
            "model": kwargs.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return SimpleNamespace(model_dump=lambda: data)


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", EchoClient)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup_router(client: AsyncTestClient) -> tuple[str, str, str]:
    """Credential + team + two chat models + router 'auto'. Returns
    (inference api key, team id, admin JWT)."""
    admin = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    cred = (
        await client.post(
            "/credentials",
            json={"name": "c-openai", "provider": "openai", "values": {"api_key": "x"}},
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
    for name, upstream in (("cheap-model", "gpt-4o-mini"), ("big-model", "gpt-4o")):
        resp = await client.post(
            f"/teams/{team}/models",
            json={
                "name": name,
                "provider": "openai",
                "credential_id": cred,
                "type": "chat",
                "provider_model_id": upstream,
            },
            headers=_bearer(admin),
        )
        assert resp.status_code == HTTP_201_CREATED, resp.text
    router = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "auto",
            "default_model": "big-model",
            "candidates": [
                {
                    "model_name": "cheap-model",
                    "description": "small and fast",
                    "quality_tier": "SIMPLE",
                },
                {
                    "model_name": "big-model",
                    "description": "large, tools-capable",
                    "quality_tier": "COMPLEX",
                    "supports_tools": True,
                },
            ],
        },
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def _chat(client: AsyncTestClient, key: str, **payload) -> dict:
    body = {"model": "auto", "messages": [{"role": "user", "content": "Ciao, grazie!"}]}
    body.update(payload)
    resp = await client.post("/v1/chat/completions", json=body, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK, resp.text
    return resp.json()


async def test_simple_prompt_routes_to_cheap_model(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_router(client)
    assert (await _chat(client, key))["model"] == "gpt-4o-mini"


async def test_complex_prompt_routes_to_big_model(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_router(client)
    body = await _chat(
        client,
        key,
        messages=[
            {
                "role": "user",
                "content": "Design a scalable distributed architecture: implement the "
                "python api with authentication, encryption and low latency database queries",
            }
        ],
    )
    assert body["model"] == "gpt-4o"


async def test_capability_filter_overrides_tier(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_router(client)
    # Simple prompt, but tools are requested → only big-model survives.
    body = await _chat(
        client,
        key,
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
    )
    assert body["model"] == "gpt-4o"


async def test_no_capable_candidate_is_a_clear_400(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_router(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                    ],
                }
            ],
        },
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST  # no vision candidate configured


async def test_broken_strategy_falls_back_to_default_model(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4 regression: a strategy blowing up must never fail the request."""

    class BrokenStrategy:
        def __init__(self, config) -> None: ...

        async def select(self, ctx, candidates):
            raise RuntimeError("boom")

    key, _, _ = await _setup_router(client)
    monkeypatch.setitem(routing_service.STRATEGIES, "complexity", BrokenStrategy)
    assert (await _chat(client, key))["model"] == "gpt-4o"  # default_model


async def test_router_decisions_are_persisted(client: AsyncTestClient, tmp_path: Path) -> None:
    key, _, _ = await _setup_router(client)
    await _chat(client, key)
    # Phase 1 has no read API yet (that's phase 3); assert on the table itself.
    import sqlite3

    rows = (
        sqlite3.connect(tmp_path / "routing.db")
        .execute(
            "SELECT router_name, strategy, chosen_model, is_shadow, fallback_used"
            " FROM routing_decision"
        )
        .fetchall()
    )
    assert rows == [("auto", "complexity", "cheap-model", 0, 0)]


async def test_router_name_cannot_shadow_a_model(client: AsyncTestClient) -> None:
    key, team, admin = await _setup_router(client)
    resp = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "cheap-model",
            "default_model": "big-model",
            "candidates": [
                {"model_name": "big-model", "description": "d", "quality_tier": "MEDIUM"}
            ],
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == 409
