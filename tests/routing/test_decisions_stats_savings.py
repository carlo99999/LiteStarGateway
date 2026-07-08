"""Phase 3 smart routing: decision list, stats, and estimated savings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.config import Settings
from litestar_gateway.domain.routing import CandidateModel, QualityTier
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"

COMPLEX_PROMPT = (
    "Design a scalable distributed architecture: implement the python api with "
    "authentication, encryption and low latency database queries"
)


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
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return SimpleNamespace(model_dump=lambda: data)


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", EchoClient)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'phase3.db'}",
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
    """Returns (inference key, team id, router id, admin JWT). Candidate
    profiles carry costs so savings are computable."""
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
    router = (
        await client.post(
            f"/teams/{team}/routers",
            json={
                "name": "auto",
                "default_model": "big-model",
                "candidates": [
                    {
                        "model_name": "cheap-model",
                        "description": "small",
                        "quality_tier": "SIMPLE",
                        "input_cost_per_token": 1e-6,
                        "output_cost_per_token": 2e-6,
                    },
                    {
                        "model_name": "big-model",
                        "description": "large",
                        "quality_tier": "COMPLEX",
                        "input_cost_per_token": 1e-5,
                        "output_cost_per_token": 2e-5,
                    },
                ],
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, router, admin


async def _chat(client: AsyncTestClient, key: str, prompt: str) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": prompt}]},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text


async def test_decision_list_with_filters_and_usage(client: AsyncTestClient) -> None:
    key, team, router, admin = await _setup(client)
    await _chat(client, key, "Ciao, grazie!")
    await _chat(client, key, COMPLEX_PROMPT)

    url = f"/teams/{team}/routers/{router}/decisions"
    rows = (await client.get(url, headers=_bearer(admin))).json()
    assert [r["chosen_model"] for r in rows] == ["big-model", "cheap-model"]  # newest first
    # Actual usage attached after settlement.
    assert rows[0]["prompt_tokens"] == 10 and rows[0]["completion_tokens"] == 5

    filtered = (await client.get(f"{url}?model=cheap-model", headers=_bearer(admin))).json()
    assert len(filtered) == 1 and filtered[0]["tier"] == "SIMPLE"
    assert (await client.get(f"{url}?limit=1", headers=_bearer(admin))).json()[0][
        "chosen_model"
    ] == "big-model"


async def test_stats_distribution(client: AsyncTestClient) -> None:
    key, team, router, admin = await _setup(client)
    await _chat(client, key, "Ciao, grazie!")
    await _chat(client, key, "Cos'è una mela?")
    await _chat(client, key, COMPLEX_PROMPT)

    stats = (
        await client.get(f"/teams/{team}/routers/{router}/stats", headers=_bearer(admin))
    ).json()
    assert stats["total"] == 3
    assert stats["by_model"] == {"cheap-model": 2, "big-model": 1}
    assert stats["by_tier"] == {"SIMPLE": 2, "COMPLEX": 1}


async def test_savings_use_actual_tokens_and_unit_cost_delta(
    client: AsyncTestClient,
) -> None:
    key, team, router, admin = await _setup(client)
    await _chat(client, key, "Ciao, grazie!")  # cheap: saves vs big
    await _chat(client, key, COMPLEX_PROMPT)  # big: alt == chosen, saves 0

    body = (
        await client.get(f"/teams/{team}/routers/{router}/savings", headers=_bearer(admin))
    ).json()
    # (1e-5-1e-6)*10 prompt + (2e-5-2e-6)*5 completion = 9e-5 + 9e-5 = 1.8e-4
    assert body["decisions_counted"] == 2
    assert body["decisions_without_usage"] == 0
    assert body["estimated_savings"] == pytest.approx(1.8e-4)


def _candidate(
    name: str, input_cost: float | None = None, output_cost: float | None = None
) -> CandidateModel:
    return CandidateModel(
        model_name=name,
        description=name,
        quality_tier=QualityTier.SIMPLE,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
    )


def test_unit_costs_alt_includes_output_only_priced_candidate() -> None:
    """R6-M41: a candidate priced only on output tokens still competes for the
    most-expensive-alternative slot; its missing input side reads as 0.0."""
    candidates = (
        _candidate("cheap", input_cost=1e-6, output_cost=2e-6),
        _candidate("out-only", output_cost=5e-5),
    )
    assert RouterService._unit_costs("cheap", candidates) == (1e-6, 2e-6, 0.0, 5e-5)


def test_unit_costs_excludes_fully_unpriced_candidates() -> None:
    candidates = (_candidate("a"), _candidate("b"))
    assert RouterService._unit_costs("a", candidates) == (None, None, None, None)


def test_unit_costs_fully_priced_candidates_unchanged() -> None:
    candidates = (
        _candidate("cheap", input_cost=1e-6, output_cost=2e-6),
        _candidate("big", input_cost=1e-5, output_cost=2e-5),
    )
    assert RouterService._unit_costs("cheap", candidates) == (1e-6, 2e-6, 1e-5, 2e-5)


async def test_observability_requires_usage_read(client: AsyncTestClient) -> None:
    key, team, router, admin = await _setup(client)
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": "plain@corp.com",
            "password": "Sup3r-Secret!",  # pragma: allowlist secret
        },
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "plain@corp.com", "role": "member"},
        headers=_bearer(admin),
    )
    member = (
        await client.post(
            "/login",
            json={
                "email": "plain@corp.com",
                "password": "Sup3r-Secret!",  # pragma: allowlist secret
            },
        )
    ).json()["access_token"]

    for path in ("decisions", "stats", "savings"):
        resp = await client.get(f"/teams/{team}/routers/{router}/{path}", headers=_bearer(member))
        assert resp.status_code == HTTP_403_FORBIDDEN, path
