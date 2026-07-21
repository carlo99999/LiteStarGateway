"""Phase 3 smart routing: decision list, stats, and estimated savings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from _invite_helpers import seed_team_and_invite
from advanced_alchemy.extensions.litestar import base
from litestar.status_codes import HTTP_200_OK, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.app import create_app
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.config import Settings
from litestar_gateway.domain.routing import CandidateModel, QualityTier
from litestar_gateway.infrastructure.llm import openai_adapter
from litestar_gateway.infrastructure.persistence.orm import RoutingDecisionModel
from litestar_gateway.infrastructure.persistence.router_repository import (
    SQLAlchemyRoutingDecisionLog,
)

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


async def test_recreated_router_name_does_not_inherit_old_decisions(
    client: AsyncTestClient,
) -> None:
    # ISSUE-001: decisions are keyed by router id, not name. Deleting "auto" and
    # recreating a router with the same name must NOT surface the old router's
    # decisions/stats under the new router.
    key, team, old_router, admin = await _setup(client)
    await _chat(client, key, COMPLEX_PROMPT)
    old_rows = (
        await client.get(f"/teams/{team}/routers/{old_router}/decisions", headers=_bearer(admin))
    ).json()
    assert len(old_rows) == 1

    assert (
        await client.delete(f"/teams/{team}/routers/{old_router}", headers=_bearer(admin))
    ).status_code in (200, 204)

    new_router = (
        await client.post(
            f"/teams/{team}/routers",
            json={
                "name": "auto",  # same name, freed by the delete
                "default_model": "big-model",
                "candidates": [
                    {"model_name": "big-model", "description": "large", "quality_tier": "COMPLEX"}
                ],
            },
            headers=_bearer(admin),
        )
    ).json()["id"]
    assert new_router != old_router

    new_rows = (
        await client.get(f"/teams/{team}/routers/{new_router}/decisions", headers=_bearer(admin))
    ).json()
    assert new_rows == []
    stats = (
        await client.get(f"/teams/{team}/routers/{new_router}/stats", headers=_bearer(admin))
    ).json()
    assert stats["total"] == 0


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


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


_ROUTER_ID = uuid4()


def _decision(team_id: UUID, **overrides: object) -> RoutingDecisionModel:
    fields: dict[str, object] = {
        "id": uuid4(),
        "team_id": team_id,
        "router_id": _ROUTER_ID,
        "router_name": "auto",
        "strategy": "heuristic",
        "chosen_model": "cheap-model",
        "chosen_input_cost": 1e-6,
        "chosen_output_cost": 2e-6,
        "alt_input_cost": 1e-5,
        "alt_output_cost": 2e-5,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        **overrides,
    }
    return RoutingDecisionModel(**fields)


async def test_savings_excludes_row_with_null_completion_tokens(session: AsyncSession) -> None:
    team_id = uuid4()
    session.add(_decision(team_id))
    session.add(_decision(team_id, completion_tokens=None))
    await session.commit()

    total, counted, without_usage = await SQLAlchemyRoutingDecisionLog(session).savings(
        team_id, _ROUTER_ID
    )
    # The NULL-completion row cannot be priced: out of the SUM *and* the count.
    assert counted == 1
    assert without_usage == 1
    assert total == pytest.approx(1.8e-4)


@pytest.mark.parametrize("field", ["alt_output_cost", "chosen_output_cost"])
async def test_savings_excludes_one_sided_output_cost_symmetrically(
    session: AsyncSession, field: str
) -> None:
    team_id = uuid4()
    session.add(_decision(team_id))
    session.add(_decision(team_id, **{field: None}))
    await session.commit()

    total, counted, without_usage = await SQLAlchemyRoutingDecisionLog(session).savings(
        team_id, _ROUTER_ID
    )
    # A NULL output cost on either side excludes the row the same way.
    assert counted == 1
    assert without_usage == 1
    assert total == pytest.approx(1.8e-4)


async def test_savings_counts_fully_priced_rows(session: AsyncSession) -> None:
    team_id = uuid4()
    session.add(_decision(team_id))
    session.add(_decision(team_id))
    await session.commit()

    total, counted, without_usage = await SQLAlchemyRoutingDecisionLog(session).savings(
        team_id, _ROUTER_ID
    )
    assert counted == 2
    assert without_usage == 0
    assert total == pytest.approx(3.6e-4)


async def test_platform_and_team_savings_aggregate_across_scopes(
    session: AsyncSession,
) -> None:
    # Two teams, two routers: team_savings sums one team's routers; platform
    # sums everything. Shadow rows never count.
    team_a, team_b = uuid4(), uuid4()
    session.add(_decision(team_a))
    session.add(_decision(team_a, router_name="other"))
    session.add(_decision(team_b))
    session.add(_decision(team_b, is_shadow=True))
    await session.commit()

    log = SQLAlchemyRoutingDecisionLog(session)
    team_total, team_counted, _ = await log.team_savings(team_a)
    assert team_counted == 2
    assert team_total == pytest.approx(3.6e-4)

    platform_total, platform_counted, _ = await log.platform_savings()
    assert platform_counted == 3
    assert platform_total == pytest.approx(5.4e-4)


async def test_platform_savings_requires_platform_admin(client: AsyncTestClient) -> None:
    _key, team, _router, admin = await _setup(client)
    resp = await client.get("/routing/savings", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert {"estimated_savings", "decisions_counted", "decisions_without_usage"} <= set(body)

    # A plain (non-platform-admin) user is refused.
    invite = await seed_team_and_invite(client, admin)
    await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": "pleb@corp.com",
            "password": "Sup3r-Secret!",  # pragma: allowlist secret
        },
    )
    member = (
        await client.post(
            "/login",
            json={
                "email": "pleb@corp.com",
                "password": "Sup3r-Secret!",  # pragma: allowlist secret
            },
        )
    ).json()["access_token"]
    resp = await client.get("/routing/savings", headers=_bearer(member))
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_team_savings_endpoint_requires_usage_read(client: AsyncTestClient) -> None:
    _key, team, _router, admin = await _setup(client)
    # The platform admin (also the team admin here) reads the team aggregate.
    resp = await client.get(f"/teams/{team}/savings", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["team_id"] == team

    # A plain member holds no usage:read -> 403 (deliberate role design).
    invite = await seed_team_and_invite(client, admin)
    await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": "plain2@corp.com",
            "password": "Sup3r-Secret!",  # pragma: allowlist secret
        },
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "plain2@corp.com", "role": "member"},
        headers=_bearer(admin),
    )
    member = (
        await client.post(
            "/login",
            json={
                "email": "plain2@corp.com",
                "password": "Sup3r-Secret!",  # pragma: allowlist secret
            },
        )
    ).json()["access_token"]
    resp = await client.get(f"/teams/{team}/savings", headers=_bearer(member))
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_observability_requires_usage_read(client: AsyncTestClient) -> None:
    key, team, router, admin = await _setup(client)
    invite = await seed_team_and_invite(client, admin)
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
