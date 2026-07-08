"""Phase 4 smart routing: S3 semantic routes via embeddings."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.routing import embeddings
from litestar_gateway.application.routing.embeddings import EmbeddingsStrategy
from litestar_gateway.config import Settings
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RoutingContext
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"

CANDIDATES = (
    CandidateModel(model_name="cheap", description="d", quality_tier=QualityTier.SIMPLE),
    CandidateModel(model_name="big", description="d", quality_tier=QualityTier.COMPLEX),
)


def _ctx(text: str) -> RoutingContext:
    return RoutingContext(
        user_text=text,
        system_prompt=None,
        estimated_input_tokens=3,
        has_images=False,
        has_tools=False,
        wants_json_schema=False,
        requested_max_tokens=None,
        default_model="big",
    )


def _vec(text: str) -> list[float]:
    """Deterministic toy embedding: greetings on one axis, the rest on the other."""
    greeting = any(w in text.lower() for w in ("ciao", "hello", "come stai"))
    return [1.0, 0.0] if greeting else [0.0, 1.0]


def _config(threshold: float = 0.8) -> dict:
    return {
        "embedding_model": f"embedder-{uuid4()}",  # unique → no cross-test cache hits
        "routes": [
            {
                "name": "smalltalk",
                "target_model": "cheap",
                "utterances": ["ciao come stai", "hello there"],
                "threshold": threshold,
            }
        ],
    }


def _strategy(config: dict, calls: list | None = None) -> EmbeddingsStrategy:
    async def embed(model_name: str, texts: list[str]) -> list[list[float]]:
        if calls is not None:
            calls.append(list(texts))
        return [_vec(t) for t in texts]

    return EmbeddingsStrategy(config, embed=embed)


# ── Unit ─────────────────────────────────────────────────────────────────────


async def test_matching_route_wins_and_reports_score() -> None:
    decision = await _strategy(_config()).select(_ctx("Ciao!"), CANDIDATES)
    assert decision.model_name == "cheap"
    assert decision.strategy == "embeddings"
    assert decision.score == pytest.approx(1.0)
    assert any("smalltalk" in s for s in decision.signals)


async def test_below_threshold_falls_to_default_model() -> None:
    decision = await _strategy(_config()).select(_ctx("analizza il bilancio"), CANDIDATES)
    assert decision.model_name == "big"  # ctx.default_model
    assert decision.score is None
    assert any("below" in s for s in decision.signals)


async def test_utterance_embeddings_are_cached_lazily() -> None:
    calls: list[list[str]] = []
    strategy = _strategy(_config(), calls)
    await strategy.select(_ctx("Ciao!"), CANDIDATES)
    await strategy.select(_ctx("hello there friend"), CANDIDATES)
    # First call: query + utterances; second call: query only.
    utterance_batches = [c for c in calls if len(c) == 2]
    assert len(utterance_batches) == 1
    assert len(calls) == 3


def _fresh_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "_ROUTE_CACHE", OrderedDict())
    monkeypatch.setattr(embeddings, "_CACHE_LOCKS", {})


async def test_insertion_past_bound_evicts_oldest_entry_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_caches(monkeypatch)
    strategies = [_strategy(_config()) for _ in range(embeddings.MAX_CACHE_ENTRIES + 1)]
    for strategy in strategies:
        await strategy.select(_ctx("Ciao!"), CANDIDATES)
    oldest = strategies[0]._cache_key
    assert oldest not in embeddings._ROUTE_CACHE
    assert oldest not in embeddings._CACHE_LOCKS
    assert len(embeddings._ROUTE_CACHE) == embeddings.MAX_CACHE_ENTRIES
    assert strategies[-1]._cache_key in embeddings._ROUTE_CACHE


async def test_cache_hit_refreshes_recency(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_caches(monkeypatch)
    first = _strategy(_config())
    await first.select(_ctx("Ciao!"), CANDIDATES)
    others = [_strategy(_config()) for _ in range(embeddings.MAX_CACHE_ENTRIES - 1)]
    for strategy in others:
        await strategy.select(_ctx("Ciao!"), CANDIDATES)
    # Cache is full and `first` is oldest; a hit must refresh its recency.
    await first.select(_ctx("Ciao!"), CANDIDATES)
    await _strategy(_config()).select(_ctx("Ciao!"), CANDIDATES)
    assert first._cache_key in embeddings._ROUTE_CACHE
    assert others[0]._cache_key not in embeddings._ROUTE_CACHE


async def test_concurrent_access_computes_config_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _fresh_caches(monkeypatch)
    calls: list[list[str]] = []

    async def embed(model_name: str, texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        await asyncio.sleep(0)
        return [_vec(t) for t in texts]

    strategy = EmbeddingsStrategy(_config(), embed=embed)
    await asyncio.gather(*(strategy.select(_ctx("Ciao!"), CANDIDATES) for _ in range(5)))
    utterance_batches = [c for c in calls if len(c) == 2]
    assert len(utterance_batches) == 1


@pytest.mark.parametrize(
    "config",
    [
        {},  # no embedding model
        {"embedding_model": "e"},  # no routes
        {"embedding_model": "e", "routes": []},
        {"embedding_model": "e", "routes": [{"name": "r", "target_model": "m"}]},  # no utterances
        {
            "embedding_model": "e",
            "routes": [{"name": "r", "target_model": "m", "utterances": ["x"], "threshold": 2}],
        },
    ],
)
def test_config_validation(config: dict) -> None:
    with pytest.raises(ValueError):
        EmbeddingsStrategy(config)


# ── Integration ──────────────────────────────────────────────────────────────


class FakeOpenAI:
    """Chat echoes the model; embeddings return the toy vectors above."""

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=self)
        self.embeddings = SimpleNamespace(create=self._embed)

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

    async def _embed(self, **kwargs):
        data = {
            "object": "list",
            "model": kwargs.get("model"),
            "data": [
                {"object": "embedding", "index": i, "embedding": _vec(t)}
                for i, t in enumerate(kwargs.get("input", []))
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        return SimpleNamespace(model_dump=lambda: data)


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", FakeOpenAI)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'phase4.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient, embedder_name: str) -> tuple[str, str, str]:
    admin = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    cred = (
        await client.post(
            "/credentials",
            json={"name": "c", "provider": "openai", "values": {"api_key": "x"}},
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
    for name, upstream, mtype in (
        ("cheap-model", "gpt-4o-mini", "chat"),
        ("big-model", "gpt-4o", "chat"),
        (embedder_name, "text-embedding-3-small", "embeddings"),
    ):
        resp = await client.post(
            f"/teams/{team}/models",
            json={
                "name": name,
                "provider": "openai",
                "credential_id": cred,
                "type": mtype,
                "provider_model_id": upstream,
            },
            headers=_bearer(admin),
        )
        assert resp.status_code == HTTP_201_CREATED, resp.text
    return admin, team, cred


def _router_body(embedder_name: str) -> dict:
    return {
        "name": "auto",
        "default_model": "big-model",
        "strategy": "embeddings",
        "strategy_config": {
            "embedding_model": embedder_name,
            "routes": [
                {
                    "name": "smalltalk",
                    "target_model": "cheap-model",
                    "utterances": ["ciao come stai", "hello there"],
                    "threshold": 0.8,
                }
            ],
        },
        "candidates": [
            {"model_name": "cheap-model", "description": "small", "quality_tier": "SIMPLE"},
            {"model_name": "big-model", "description": "large", "quality_tier": "COMPLEX"},
        ],
    }


async def test_semantic_routing_end_to_end(client: AsyncTestClient) -> None:
    embedder = f"embedder-{uuid4()}"
    admin, team, _ = await _setup(client, embedder)
    resp = await client.post(
        f"/teams/{team}/routers", json=_router_body(embedder), headers=_bearer(admin)
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]

    async def chat(prompt: str) -> str:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": prompt}]},
            headers=_bearer(key),
        )
        assert r.status_code == HTTP_200_OK, r.text
        return r.json()["model"]

    assert await chat("Ciao, come stai?") == "gpt-4o-mini"  # matches smalltalk route
    assert await chat("Analizza il bilancio trimestrale") == "gpt-4o"  # below threshold


async def test_router_rejects_non_embeddings_model_and_bad_target(
    client: AsyncTestClient,
) -> None:
    embedder = f"embedder-{uuid4()}"
    admin, team, _ = await _setup(client, embedder)
    body = _router_body(embedder)
    body["strategy_config"]["embedding_model"] = "cheap-model"  # chat, not embeddings
    resp = await client.post(f"/teams/{team}/routers", json=body, headers=_bearer(admin))
    assert resp.status_code == HTTP_400_BAD_REQUEST

    body = _router_body(embedder)
    body["strategy_config"]["routes"][0]["target_model"] = "not-a-candidate"
    resp = await client.post(f"/teams/{team}/routers", json=body, headers=_bearer(admin))
    assert resp.status_code == HTTP_400_BAD_REQUEST
