"""R7-H22: judge/embeddings routing strategies make real, billable provider
calls. Those calls must go through UsageMeter — billed as a usage_event and
budget-gated — not straight through the gateway unmetered."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.llm import openai_adapter

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


class JudgeAwareClient:
    """Fake AsyncOpenAI: a judge request (json_schema response_format) gets a
    valid `{"choice": ...}`; everything else echoes the model with usage."""

    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=self)

    async def close(self) -> None:
        return None

    async def create(self, **kwargs):
        is_judge = (kwargs.get("response_format") or {}).get("type") == "json_schema"
        content = json.dumps({"choice": "cheap-model"}) if is_judge else "ok"
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
            # Distinct token counts so the judge's own usage is identifiable.
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
            if is_judge
            else {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return SimpleNamespace(model_dump=lambda: data)


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncTestClient]:
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", JudgeAwareClient)
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


async def _setup_judge_router(client: AsyncTestClient) -> tuple[str, str, str]:
    """Credential + team + three chat models (cheap, big, judge) + a judge
    router. Returns (inference key, team id, admin JWT)."""
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
    # Give the judge and big models a real (nonzero) price so spend is visible.
    for name, upstream, in_cost, out_cost in (
        ("cheap-model", "gpt-4o-mini", 0.0, 0.0),
        ("big-model", "gpt-4o", 0.001, 0.002),
        ("judge-model", "gpt-4o-mini", 0.001, 0.002),
    ):
        resp = await client.post(
            f"/teams/{team}/models",
            json={
                "name": name,
                "provider": "openai",
                "credential_id": cred,
                "type": "chat",
                "provider_model_id": upstream,
                "input_cost_per_token": in_cost,
                "output_cost_per_token": out_cost,
            },
            headers=_bearer(admin),
        )
        assert resp.status_code == HTTP_201_CREATED, resp.text
    router = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "auto",
            "default_model": "big-model",
            "strategy": "judge",
            "strategy_config": {"judge_model": "judge-model"},
            "candidates": [
                {
                    "model_name": "cheap-model",
                    "description": "small+fast",
                    "quality_tier": "SIMPLE",
                },
                {"model_name": "big-model", "description": "strong", "quality_tier": "COMPLEX"},
            ],
        },
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def test_judge_call_is_billed_as_usage_event(
    client: AsyncTestClient, tmp_path: Path
) -> None:
    key, _, _ = await _setup_judge_router(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "Ciao, grazie!"}]},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    # The judge picked cheap-model → that's the answered model.
    assert resp.json()["model"] == "gpt-4o-mini"

    rows = (
        sqlite3.connect(tmp_path / "routing.db")
        .execute(
            "SELECT operation, model_name, prompt_tokens, completion_tokens"
            " FROM usage_event ORDER BY operation"
        )
        .fetchall()
    )
    operations = {r[0] for r in rows}
    # The judge's own provider call must be billed, not just the routed answer.
    assert "routing.judge" in operations, f"judge call not billed; events={rows}"
    judge_event = next(r for r in rows if r[0] == "routing.judge")
    assert judge_event[1] == "judge-model"
    assert judge_event[2] == 11 and judge_event[3] == 3  # the judge's own tokens


async def test_over_budget_team_does_not_run_the_judge(
    client: AsyncTestClient, tmp_path: Path
) -> None:
    key, team, admin = await _setup_judge_router(client)
    # Warm up committed spend on the priced big-model (a direct, non-routed call),
    # then set the budget below it. The team is now over budget.
    warmup = await client.post(
        "/v1/chat/completions",
        json={"model": "big-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert warmup.status_code == HTTP_200_OK, warmup.text
    resp = await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 0.0001, "window": "monthly"},
        headers=_bearer(admin),
    )
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED), resp.text

    # Now route through the judge: its provider call must be budget-gated, so it
    # falls through §4 to default_model rather than spending un-gated on the judge.
    await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    rows = (
        sqlite3.connect(tmp_path / "routing.db")
        .execute("SELECT operation FROM usage_event WHERE operation = 'routing.judge'")
        .fetchall()
    )
    assert rows == [], f"judge ran despite an exhausted budget: {rows}"
