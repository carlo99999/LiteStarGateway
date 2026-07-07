"""Phase 5 smart routing: S4 LLM judge, S5 hybrid gray-zone, S6 export."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.routing.hybrid import HybridStrategy
from litestar_gateway.application.routing.judge import JUDGE_PROMPT_V1, JudgeStrategy
from litestar_gateway.config import Settings
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RoutingContext
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"

CANDIDATES = (
    CandidateModel(model_name="cheap", description="small+fast", quality_tier=QualityTier.SIMPLE),
    CandidateModel(model_name="big", description="strongest", quality_tier=QualityTier.COMPLEX),
)


def _ctx(text: str) -> RoutingContext:
    return RoutingContext(
        user_text=text,
        system_prompt=None,
        estimated_input_tokens=len(text) // 4,
        has_images=False,
        has_tools=False,
        wants_json_schema=False,
        requested_max_tokens=None,
        default_model="big",
    )


# ── S4 judge: unit ───────────────────────────────────────────────────────────


def _judge(response_choice: str, seen: dict | None = None) -> JudgeStrategy:
    async def complete(model_name: str, request: dict) -> dict:
        if seen is not None:
            seen.update(request)
        return {"choices": [{"message": {"content": json.dumps({"choice": response_choice})}}]}

    return JudgeStrategy({"judge_model": "mini", "char_budget": 20}, complete=complete)


async def test_judge_constrained_request_and_choice() -> None:
    seen: dict = {}
    decision = await _judge("big", seen).select(_ctx("x" * 100), CANDIDATES)
    assert decision.model_name == "big"
    assert decision.strategy == "judge"

    schema = seen["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["choice"]["enum"] == ["cheap", "big"]  # constrained enum
    assert seen["messages"][0]["content"] == JUDGE_PROMPT_V1  # versioned constant
    user_message = seen["messages"][1]["content"]
    assert "cheap [SIMPLE]: small+fast" in user_message
    assert "x" * 20 in user_message and "x" * 21 not in user_message  # char budget


async def test_judge_rejects_non_candidate_and_malformed() -> None:
    with pytest.raises(ValueError):
        await _judge("nope").select(_ctx("hi"), CANDIDATES)

    async def broken(model_name: str, request: dict) -> dict:
        return {"choices": [{"message": {"content": "not json"}}]}

    strategy = JudgeStrategy({"judge_model": "mini"}, complete=broken)
    with pytest.raises(json.JSONDecodeError):
        await strategy.select(_ctx("hi"), CANDIDATES)


def test_judge_requires_model_config() -> None:
    with pytest.raises(ValueError):
        JudgeStrategy({})


# ── S5 hybrid: unit ──────────────────────────────────────────────────────────


class StubEscalation:
    def __init__(self) -> None:
        self.calls = 0

    async def select(self, ctx, candidates):
        self.calls += 1
        from litestar_gateway.domain.routing import RoutingDecision

        return RoutingDecision(
            model_name="big",
            strategy="judge",
            tier=None,
            score=None,
            signals=("judge stub",),
            decision_ms=0.0,
        )


async def test_hybrid_confident_case_keeps_rule_based_answer() -> None:
    stub = StubEscalation()
    strategy = HybridStrategy({"escalation_strategy": "judge", "margin": 0.08}, escalation=stub)
    # "Ciao, grazie!" scores ~-0.15 — far from every boundary.
    decision = await strategy.select(_ctx("Ciao, grazie!"), CANDIDATES)
    assert decision.model_name == "cheap"
    assert decision.strategy == "hybrid"
    assert stub.calls == 0
    assert any("gray-zone: no" in s for s in decision.signals)


async def test_hybrid_gray_zone_escalates() -> None:
    stub = StubEscalation()
    # Huge margin: every score is a gray zone → escalation must fire.
    strategy = HybridStrategy({"escalation_strategy": "judge", "margin": 5.0}, escalation=stub)
    decision = await strategy.select(_ctx("Ciao, grazie!"), CANDIDATES)
    assert decision.model_name == "big"  # the escalation's verdict
    assert stub.calls == 1
    assert any("escalated to judge" in s for s in decision.signals)


def test_hybrid_requires_known_escalation() -> None:
    with pytest.raises(ValueError):
        HybridStrategy({"escalation_strategy": "complexity"})
    with pytest.raises(ValueError):
        HybridStrategy({"escalation_strategy": "judge", "margin": -1})


# ── Integration: judge end-to-end + S6 export ────────────────────────────────


class FakeOpenAI:
    """Judge calls (response_format present) pick cheap-model; chat echoes."""

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=self)

    async def close(self) -> None:
        return None

    async def create(self, **kwargs):
        if kwargs.get("response_format"):  # the judge's constrained call
            content = json.dumps({"choice": "cheap-model"})
        else:
            content = "ok"
        data = {
            "id": "cmpl-x",
            "object": "chat.completion",
            "model": kwargs.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return SimpleNamespace(model_dump=lambda: data)


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", FakeOpenAI)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'phase5.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient) -> tuple[str, str, str, str]:
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
    for name, upstream in (
        ("cheap-model", "gpt-4o-mini"),
        ("big-model", "gpt-4o"),
        ("judge-model", "gpt-4o-mini"),
    ):
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
    router = (
        await client.post(
            f"/teams/{team}/routers",
            json={
                "name": "auto",
                "default_model": "big-model",
                "strategy": "judge",
                "strategy_config": {"judge_model": "judge-model"},
                "candidates": [
                    {"model_name": "cheap-model", "description": "small", "quality_tier": "SIMPLE"},
                    {"model_name": "big-model", "description": "large", "quality_tier": "COMPLEX"},
                ],
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, router, admin


async def test_judge_routes_end_to_end_and_export_has_text(client: AsyncTestClient) -> None:
    key, team, router, admin = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "Qualsiasi cosa"}]},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["model"] == "gpt-4o-mini"  # the judge's verdict

    rows = (
        await client.get(f"/teams/{team}/routers/{router}/decisions", headers=_bearer(admin))
    ).json()
    assert rows[0]["strategy"] == "judge"

    export = await client.get(
        f"/teams/{team}/routers/{router}/decisions/export", headers=_bearer(admin)
    )
    assert export.status_code == HTTP_200_OK
    (line,) = export.text.strip().splitlines()
    payload = json.loads(line)
    assert payload["text"] == "Qualsiasi cosa"
    assert payload["chosen_model"] == "cheap-model"
    assert payload["strategy"] == "judge"


async def test_judge_validation_rejects_missing_model(client: AsyncTestClient) -> None:
    key, team, router, admin = await _setup(client)
    resp = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "auto2",
            "default_model": "big-model",
            "strategy": "judge",
            "strategy_config": {"judge_model": "does-not-exist"},
            "candidates": [
                {"model_name": "big-model", "description": "d", "quality_tier": "COMPLEX"}
            ],
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST
