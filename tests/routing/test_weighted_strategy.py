"""Weighted multi-model routing: percentage-split random selection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.routing.weighted import WeightedStrategy
from litestar_gateway.config import Settings
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RoutingContext
from litestar_gateway.infrastructure.llm import openai_adapter


def _candidate(name: str, weight: float | None) -> CandidateModel:
    return CandidateModel(
        model_name=name, description=name, quality_tier=QualityTier.MEDIUM, weight=weight
    )


CTX = RoutingContext(
    user_text="hi",
    system_prompt=None,
    estimated_input_tokens=1,
    has_images=False,
    has_tools=False,
    wants_json_schema=False,
    requested_max_tokens=None,
)


def _draws(*values: float):
    it = iter(values)

    def draw() -> float:
        return next(it)

    return draw


# ── Unit: pure selection ─────────────────────────────────────────────────────


async def test_selects_by_cumulative_weight_boundaries() -> None:
    candidates = (_candidate("a", 30), _candidate("b", 70))  # total 100
    strategy = WeightedStrategy({}, random_fn=_draws(0.0))
    assert (await strategy.select(CTX, candidates)).model_name == "a"

    strategy = WeightedStrategy({}, random_fn=_draws(0.2999))
    assert (await strategy.select(CTX, candidates)).model_name == "a"

    strategy = WeightedStrategy({}, random_fn=_draws(0.3001))
    assert (await strategy.select(CTX, candidates)).model_name == "b"

    strategy = WeightedStrategy({}, random_fn=_draws(0.9999))
    assert (await strategy.select(CTX, candidates)).model_name == "b"


async def test_reports_strategy_and_normalized_probability_as_score() -> None:
    candidates = (_candidate("a", 1), _candidate("b", 3))  # 25% / 75%
    decision = await WeightedStrategy({}, random_fn=_draws(0.1)).select(CTX, candidates)
    assert decision.model_name == "a"
    assert decision.strategy == "weighted"
    assert decision.score == pytest.approx(0.25)
    assert any("a" in s for s in decision.signals)


async def test_renormalizes_over_only_the_capability_filtered_survivors() -> None:
    # A capability filter may have already dropped a candidate upstream; the
    # strategy only ever sees survivors and must split proportionally among them
    # (30:70 of the original 3-way split becomes 30:70 of the remaining two).
    candidates = (_candidate("a", 30), _candidate("b", 70))  # "c" already filtered out
    strategy = WeightedStrategy({}, random_fn=_draws(0.31))
    assert (await strategy.select(CTX, candidates)).model_name == "b"


async def test_single_candidate_always_wins() -> None:
    candidates = (_candidate("only", 1),)
    decision = await WeightedStrategy({}, random_fn=_draws(0.9)).select(CTX, candidates)
    assert decision.model_name == "only"


def test_requires_positive_weights_on_every_candidate() -> None:
    for candidates in (
        (_candidate("a", None), _candidate("b", 1)),  # missing weight
        (_candidate("a", 0), _candidate("b", 1)),  # zero weight
        (_candidate("a", -1), _candidate("b", 1)),  # negative weight
    ):
        with pytest.raises(ValueError):
            import asyncio

            asyncio.run(WeightedStrategy({}).select(CTX, candidates))


# ── Integration: end-to-end split + validation ───────────────────────────────


class EchoClient:
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


MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", EchoClient)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'weighted.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient, candidates: list[dict]) -> tuple[str, str, str]:
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
    names = [c["model_name"] for c in candidates]
    for name in names:
        await client.post(
            f"/teams/{team}/models",
            json={
                "name": name,
                "provider": "openai",
                "credential_id": cred,
                "type": "chat",
                "provider_model_id": name,
            },
            headers=_bearer(admin),
        )
    router = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "split",
            "default_model": names[0],
            "strategy": "weighted",
            "candidates": candidates,
        },
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def test_weighted_split_end_to_end_over_many_requests(client: AsyncTestClient) -> None:
    key, _, _ = await _setup(
        client,
        [
            {"model_name": "a", "description": "a", "quality_tier": "MEDIUM", "weight": 50},
            {"model_name": "b", "description": "b", "quality_tier": "MEDIUM", "weight": 50},
        ],
    )
    chosen: dict[str, int] = {}
    for _ in range(40):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "split", "messages": [{"role": "user", "content": "hi"}]},
            headers=_bearer(key),
        )
        assert resp.status_code == HTTP_200_OK
        model = resp.json()["model"]
        chosen[model] = chosen.get(model, 0) + 1

    assert set(chosen) == {"a", "b"}  # a true 50/50 split hits both over 40 draws
    assert min(chosen.values()) >= 5  # loose bound: catches an all-one-side bug


async def test_router_rejects_missing_or_nonpositive_weight(client: AsyncTestClient) -> None:
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
    for name in ("a", "b"):
        await client.post(
            f"/teams/{team}/models",
            json={
                "name": name,
                "provider": "openai",
                "credential_id": cred,
                "type": "chat",
                "provider_model_id": name,
            },
            headers=_bearer(admin),
        )
    for candidates in (
        [
            {"model_name": "a", "description": "a", "quality_tier": "MEDIUM"},  # no weight
            {"model_name": "b", "description": "b", "quality_tier": "MEDIUM", "weight": 1},
        ],
        [
            {"model_name": "a", "description": "a", "quality_tier": "MEDIUM", "weight": 0},
            {"model_name": "b", "description": "b", "quality_tier": "MEDIUM", "weight": 1},
        ],
    ):
        resp = await client.post(
            f"/teams/{team}/routers",
            json={
                "name": f"split-{candidates[0].get('weight', 'none')}",
                "default_model": "a",
                "strategy": "weighted",
                "candidates": candidates,
            },
            headers=_bearer(admin),
        )
        assert resp.status_code == HTTP_400_BAD_REQUEST, resp.text
