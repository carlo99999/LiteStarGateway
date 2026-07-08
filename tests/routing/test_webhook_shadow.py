"""Phase 2 smart routing: S2 webhook strategy + shadow mode."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

import litestar_gateway.application.routing.webhook as webhook_module
from litestar_gateway.app import create_app
from litestar_gateway.application.routing.webhook import WebhookStrategy
from litestar_gateway.config import Settings
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RoutingContext
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"

CANDIDATES = (
    CandidateModel(model_name="m1", description="d1", quality_tier=QualityTier.SIMPLE),
    CandidateModel(model_name="m2", description="d2", quality_tier=QualityTier.COMPLEX),
)


def _ctx(text: str = "hello") -> RoutingContext:
    return RoutingContext(
        user_text=text,
        system_prompt="sys",
        estimated_input_tokens=3,
        has_images=False,
        has_tools=False,
        wants_json_schema=False,
        requested_max_tokens=None,
    )


def _mock_webhook(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def factory(timeout_seconds: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout_seconds, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(webhook_module, "_client_factory", factory)


# ── S2 webhook: unit ─────────────────────────────────────────────────────────


async def test_webhook_choice_by_index_and_payload_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choice": 2})

    _mock_webhook(monkeypatch, handler)
    strategy = WebhookStrategy({"url": "https://picker.example/route", "bearer_token": "tok"})
    decision = await strategy.select(_ctx("pick one"), CANDIDATES)

    assert decision.model_name == "m2"  # 1-based index
    assert decision.strategy == "webhook"
    assert seen["payload"] == {
        "task": "pick one",
        "system_prompt": "sys",
        "models": ["m1", "m2"],
        "metadata": {"estimated_tokens": 3},
    }
    assert seen["auth"] == "Bearer tok"


async def test_webhook_choice_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_webhook(monkeypatch, lambda r: httpx.Response(200, json={"choice": "m1"}))
    decision = await WebhookStrategy({"url": "https://x/r"}).select(_ctx(), CANDIDATES)
    assert decision.model_name == "m1"


@pytest.mark.parametrize(
    "body",
    [
        {"choice": 0},  # below range (1-based)
        {"choice": 3},  # above range
        {"choice": "nope"},  # unknown name
        {"choice": True},  # bool is not an index
        {"choice": 1.5},  # wrong type
        {"pick": 1},  # missing key
        [1],  # not an object
    ],
)
async def test_webhook_rejects_malformed_choices(monkeypatch: pytest.MonkeyPatch, body) -> None:
    _mock_webhook(monkeypatch, lambda r: httpx.Response(200, json=body))
    with pytest.raises(ValueError):
        await WebhookStrategy({"url": "https://x/r"}).select(_ctx(), CANDIDATES)


async def test_webhook_non_2xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_webhook(monkeypatch, lambda r: httpx.Response(500, json={"choice": 1}))
    with pytest.raises(httpx.HTTPStatusError):
        await WebhookStrategy({"url": "https://x/r"}).select(_ctx(), CANDIDATES)


def test_webhook_requires_http_url() -> None:
    for bad in ({}, {"url": None}, {"url": "ftp://x"}, {"url": 3}):
        with pytest.raises(ValueError):
            WebhookStrategy(bad)


# ── Integration: webhook fallback + shadow mode ──────────────────────────────


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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'phase2.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient, router_extra: dict) -> tuple[str, str, str]:
    """Credential + team + two models + router 'auto' (+ overrides). Returns
    (inference key, team id, admin JWT)."""
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
        await client.post(
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
    router_body = {
        "name": "auto",
        "default_model": "big-model",
        "candidates": [
            {"model_name": "cheap-model", "description": "small", "quality_tier": "SIMPLE"},
            {"model_name": "big-model", "description": "large", "quality_tier": "COMPLEX"},
        ],
        **router_extra,
    }
    resp = await client.post(f"/teams/{team}/routers", json=router_body, headers=_bearer(admin))
    assert resp.status_code == HTTP_201_CREATED, resp.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def _chat(client: AsyncTestClient, key: str) -> dict:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "Ciao, grazie!"}]},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    return resp.json()


def _decisions(tmp_path: Path) -> list[tuple]:
    return (
        sqlite3.connect(tmp_path / "phase2.db")
        .execute(
            "SELECT strategy, chosen_model, is_shadow, fallback_used FROM routing_decision"
            " ORDER BY is_shadow"
        )
        .fetchall()
    )


async def test_unreachable_webhook_falls_back_to_default(
    client: AsyncTestClient, tmp_path: Path
) -> None:
    key, _, _ = await _setup(
        client,
        {
            "strategy": "webhook",
            "strategy_config": {"url": "http://127.0.0.1:9/route", "timeout_ms": 200},
        },
    )
    body = await _chat(client, key)
    assert body["model"] == "gpt-4o"  # default_model
    assert ("webhook", "big-model", 0, 1) in _decisions(tmp_path)


async def test_shadow_strategy_persists_alongside_active(
    client: AsyncTestClient, tmp_path: Path
) -> None:
    key, _, _ = await _setup(client, {"shadow_strategy": "complexity"})
    body = await _chat(client, key)
    assert body["model"] == "gpt-4o-mini"  # active complexity → SIMPLE

    for _ in range(40):  # fire-and-forget: poll briefly for the shadow row
        if len(_decisions(tmp_path)) == 2:
            break
        await asyncio.sleep(0.05)
    rows = _decisions(tmp_path)
    assert rows == [
        ("complexity", "cheap-model", 0, 0),
        ("complexity", "cheap-model", 1, 0),
    ]


async def test_failing_shadow_never_touches_the_request(
    client: AsyncTestClient, tmp_path: Path
) -> None:
    key, _, _ = await _setup(
        client,
        {
            "shadow_strategy": "webhook",
            "strategy_config": {"shadow": {"url": "http://127.0.0.1:9/route", "timeout_ms": 100}},
        },
    )
    body = await _chat(client, key)
    assert body["model"] == "gpt-4o-mini"
    await asyncio.sleep(0.4)  # give the failing shadow time to run and be swallowed
    assert _decisions(tmp_path) == [("complexity", "cheap-model", 0, 0)]


# ── R6-H19: bearer_token encrypted at rest + redacted from responses ─────────

WEBHOOK_TOKEN = "s3cret-webhook-token"  # pragma: allowlist secret
WEBHOOK_CONFIG = {"url": "https://picker.example/route", "bearer_token": WEBHOOK_TOKEN}


def _capture_auth(monkeypatch: pytest.MonkeyPatch, seen: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"choice": 2})

    _mock_webhook(monkeypatch, handler)


def _raw_config(tmp_path: Path) -> str:
    return (
        sqlite3.connect(tmp_path / "phase2.db")
        .execute("SELECT strategy_config FROM router")
        .fetchone()[0]
    )


async def test_webhook_token_is_encrypted_at_rest(client: AsyncTestClient, tmp_path: Path) -> None:
    await _setup(
        client,
        {
            "strategy": "webhook",
            "strategy_config": {**WEBHOOK_CONFIG, "shadow": dict(WEBHOOK_CONFIG)},
            "shadow_strategy": "webhook",
        },
    )
    raw = _raw_config(tmp_path)
    assert WEBHOOK_TOKEN not in raw
    stored = json.loads(raw)
    assert set(stored["bearer_token"]) == {"key_id", "token"}
    assert set(stored["shadow"]["bearer_token"]) == {"key_id", "token"}


async def test_responses_mask_webhook_token(client: AsyncTestClient) -> None:
    _, team, admin = await _setup(
        client, {"strategy": "webhook", "strategy_config": dict(WEBHOOK_CONFIG)}
    )
    body = {
        "name": "auto2",
        "default_model": "big-model",
        "candidates": [
            {"model_name": "cheap-model", "description": "small", "quality_tier": "SIMPLE"},
            {"model_name": "big-model", "description": "large", "quality_tier": "COMPLEX"},
        ],
        "strategy": "webhook",
        "strategy_config": {**WEBHOOK_CONFIG, "shadow": dict(WEBHOOK_CONFIG)},
        "shadow_strategy": "webhook",
    }
    create = await client.post(f"/teams/{team}/routers", json=body, headers=_bearer(admin))
    assert create.status_code == HTTP_201_CREATED, create.text
    assert WEBHOOK_TOKEN not in create.text
    config = create.json()["strategy_config"]
    assert config["bearer_token"] == "***"
    assert config["shadow"]["bearer_token"] == "***"

    listing = await client.get(f"/teams/{team}/routers", headers=_bearer(admin))
    assert WEBHOOK_TOKEN not in listing.text

    updated = await client.put(
        f"/teams/{team}/routers/{create.json()['id']}", json=body, headers=_bearer(admin)
    )
    assert updated.status_code == HTTP_200_OK, updated.text
    assert WEBHOOK_TOKEN not in updated.text


async def test_webhook_gets_decrypted_token_and_masked_update_preserves_it(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}
    _capture_auth(monkeypatch, seen)
    key, team, admin = await _setup(
        client, {"strategy": "webhook", "strategy_config": dict(WEBHOOK_CONFIG)}
    )
    await _chat(client, key)
    assert seen["auth"] == f"Bearer {WEBHOOK_TOKEN}"

    # A client naturally echoes the masked config back on update: the stored
    # token must survive, not be overwritten with the mask.
    router_id = (await client.get(f"/teams/{team}/routers", headers=_bearer(admin))).json()[0]["id"]
    body = {
        "name": "auto",
        "default_model": "big-model",
        "candidates": [
            {"model_name": "cheap-model", "description": "small", "quality_tier": "SIMPLE"},
            {"model_name": "big-model", "description": "large", "quality_tier": "COMPLEX"},
        ],
        "strategy": "webhook",
        "strategy_config": {"url": WEBHOOK_CONFIG["url"], "bearer_token": "***"},
    }
    resp = await client.put(f"/teams/{team}/routers/{router_id}", json=body, headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text

    seen.clear()
    await _chat(client, key)
    assert seen["auth"] == f"Bearer {WEBHOOK_TOKEN}"


async def test_legacy_plaintext_token_still_works_and_is_masked(
    client: AsyncTestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}
    _capture_auth(monkeypatch, seen)
    key, team, admin = await _setup(
        client, {"strategy": "webhook", "strategy_config": dict(WEBHOOK_CONFIG)}
    )
    # Simulate a row written before encryption existed: raw plaintext token.
    conn = sqlite3.connect(tmp_path / "phase2.db")
    conn.execute("UPDATE router SET strategy_config = ?", (json.dumps(WEBHOOK_CONFIG),))
    conn.commit()
    conn.close()

    await _chat(client, key)
    assert seen["auth"] == f"Bearer {WEBHOOK_TOKEN}"

    listing = await client.get(f"/teams/{team}/routers", headers=_bearer(admin))
    assert WEBHOOK_TOKEN not in listing.text
    assert listing.json()[0]["strategy_config"]["bearer_token"] == "***"


async def test_router_validation_rejects_bad_webhook_and_shadow(
    client: AsyncTestClient,
) -> None:
    key, team, admin = await _setup(client, {})
    for extra in (
        {"name": "r2", "strategy": "webhook"},  # webhook without url
        {"name": "r3", "shadow_strategy": "nope"},  # unknown shadow strategy
        {"name": "r4", "shadow_strategy": "webhook"},  # shadow webhook without url
    ):
        body = {
            "default_model": "big-model",
            "candidates": [
                {"model_name": "big-model", "description": "d", "quality_tier": "COMPLEX"}
            ],
            **extra,
        }
        resp = await client.post(f"/teams/{team}/routers", json=body, headers=_bearer(admin))
        assert resp.status_code == HTTP_400_BAD_REQUEST, (extra, resp.text)
