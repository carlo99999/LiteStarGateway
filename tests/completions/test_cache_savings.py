"""Plan 04 Phase 3: response-cache observability — hit rate + Σ(avoided cost).

Table-driven repository tests for `SQLAlchemyUsageRepository.cache_savings` /
`.platform_cache_savings` (mirroring `tests/routing/test_decisions_stats_savings.py`'s
structure for the analogous routing-savings aggregate), plus full-stack RBAC and
end-to-end tests through the `/teams/{team_id}/cache/savings` and `/cache/savings`
endpoints.
"""

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
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.llm import openai_adapter
from litestar_gateway.infrastructure.persistence.orm import (
    CredentialModel,
    ModelRecord,
    OrganizationModel,
    SecretKeyModel,
    TeamModel,
    UsageEventModel,
)
from litestar_gateway.infrastructure.persistence.usage_repository import SQLAlchemyUsageRepository

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"


class EchoClient:
    """Always returns the identical body/usage — repeat requests are cacheable."""

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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cache_savings.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        response_cache_enabled=True,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient) -> tuple[str, str, str]:
    """Returns (inference key, team id, admin JWT); the team's one model has
    `cache_enabled=True` so a repeated identical request produces a cache hit."""
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
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "cached-model",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o-mini",
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "cache_enabled": True,
        },
        headers=_bearer(admin),
    )
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]
    return key, team, admin


async def _chat(client: AsyncTestClient, key: str) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "cached-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text


async def test_cache_hit_is_reflected_in_team_cache_savings(client: AsyncTestClient) -> None:
    key, team, admin = await _setup(client)
    await _chat(client, key)  # miss: provider call, populates the cache
    await _chat(client, key)  # hit: served from cache at $0

    body = (await client.get(f"/teams/{team}/cache/savings", headers=_bearer(admin))).json()
    assert body["team_id"] == team
    assert body["total_requests"] == 2
    assert body["cache_hits"] == 1
    assert body["cache_hit_rate"] == pytest.approx(0.5)
    # (1e-6 * 10) + (2e-6 * 5) = 1e-5 + 1e-5 = 2e-5
    assert body["estimated_cost_saved"] == pytest.approx(2e-5)


async def test_team_cache_savings_empty_state_is_zero(client: AsyncTestClient) -> None:
    _key, team, admin = await _setup(client)

    body = (await client.get(f"/teams/{team}/cache/savings", headers=_bearer(admin))).json()
    assert body["total_requests"] == 0
    assert body["cache_hits"] == 0
    assert body["cache_hit_rate"] == 0.0
    assert body["estimated_cost_saved"] == 0.0


async def test_team_cache_savings_requires_usage_read(client: AsyncTestClient) -> None:
    _key, team, admin = await _setup(client)
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

    resp = await client.get(f"/teams/{team}/cache/savings", headers=_bearer(member))
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_platform_cache_savings_requires_platform_admin(client: AsyncTestClient) -> None:
    key, team, admin = await _setup(client)
    await _chat(client, key)
    await _chat(client, key)

    resp = await client.get("/cache/savings", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert {"cache_hit_rate", "estimated_cost_saved", "cache_hits", "total_requests"} <= set(body)
    assert body["cache_hits"] == 1

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
    resp = await client.get("/cache/savings", headers=_bearer(member))
    assert resp.status_code == HTTP_403_FORBIDDEN


# --- Repository-level table-driven tests -----------------------------------


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed_team(session: AsyncSession) -> UUID:
    """A real `team` row: `usage_event.team_id` is a Postgres foreign key (SQLite
    doesn't enforce it, but the CI Postgres job does), so a bare `uuid4()` fails
    there even though it passes locally."""
    org = OrganizationModel(id=uuid4(), name=f"org-{uuid4()}")
    session.add(org)
    await session.flush()
    team = TeamModel(id=uuid4(), organization_id=org.id, name=f"team-{uuid4()}")
    session.add(team)
    await session.flush()
    return team.id


async def _seed_credential(session: AsyncSession) -> UUID:
    """A real `credential` row: `model.credential_id` is a Postgres foreign key."""
    secret_key = SecretKeyModel(id=uuid4(), purpose="credential", material="k")
    session.add(secret_key)
    await session.flush()
    credential = CredentialModel(
        id=uuid4(),
        name=f"cred-{uuid4()}",
        provider="openai",
        encrypted_values="",
        key_id=secret_key.id,
    )
    session.add(credential)
    await session.flush()
    return credential.id


def _model_record(model_id: UUID, credential_id: UUID, **overrides: object) -> ModelRecord:
    fields: dict[str, object] = {
        "id": model_id,
        "team_id": None,
        "name": "m",
        "provider": "openai",
        "credential_id": credential_id,
        "type": "chat",
        "provider_model_id": "gpt-4o-mini",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 2e-6,
        **overrides,
    }
    return ModelRecord(**fields)


def _usage_event(team_id: UUID, model_id: UUID, **overrides: object) -> UsageEventModel:
    fields: dict[str, object] = {
        "team_id": team_id,
        "api_key_id": None,
        "model_id": model_id,
        "model_name": "m",
        "operation": "chat.completions",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost": 0.0,
        "cache_hit": True,
        **overrides,
    }
    return UsageEventModel(**fields)


async def test_cache_savings_empty_state_is_zero(session: AsyncSession) -> None:
    avoided, priced_hits, without_price, total = await SQLAlchemyUsageRepository(
        session
    ).cache_savings(uuid4())
    assert (avoided, priced_hits, without_price, total) == (0.0, 0, 0, 0)


async def test_cache_savings_sums_avoided_cost_for_priced_hits(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    credential_id = await _seed_credential(session)
    model_id = uuid4()
    session.add(_model_record(model_id, credential_id))
    session.add(_usage_event(team_id, model_id))  # a hit
    session.add(_usage_event(team_id, model_id, cache_hit=False, cost=1.0))  # a miss
    await session.commit()

    avoided, priced_hits, without_price, total = await SQLAlchemyUsageRepository(
        session
    ).cache_savings(team_id)
    # (1e-6 * 10) + (2e-6 * 5) = 2e-5
    assert avoided == pytest.approx(2e-5)
    assert priced_hits == 1
    assert without_price == 0
    assert total == 2


async def test_cache_savings_counts_hit_without_price_when_model_missing(
    session: AsyncSession,
) -> None:
    team_id = await _seed_team(session)
    # No ModelRecord row for this model_id: the hit is real but unpriced.
    session.add(_usage_event(team_id, uuid4()))
    await session.commit()

    avoided, priced_hits, without_price, total = await SQLAlchemyUsageRepository(
        session
    ).cache_savings(team_id)
    assert avoided == 0.0
    assert priced_hits == 0
    assert without_price == 1
    assert total == 1


async def test_cache_savings_is_tenant_isolated(session: AsyncSession) -> None:
    team_a, team_b = await _seed_team(session), await _seed_team(session)
    credential_id = await _seed_credential(session)
    model_id = uuid4()
    session.add(_model_record(model_id, credential_id))
    session.add(_usage_event(team_a, model_id))
    session.add(_usage_event(team_b, model_id))
    await session.commit()

    avoided, priced_hits, _without_price, total = await SQLAlchemyUsageRepository(
        session
    ).cache_savings(team_a)
    assert priced_hits == 1
    assert total == 1
    assert avoided == pytest.approx(2e-5)


async def test_platform_cache_savings_aggregates_across_teams(session: AsyncSession) -> None:
    team_a, team_b = await _seed_team(session), await _seed_team(session)
    credential_id = await _seed_credential(session)
    model_id = uuid4()
    session.add(_model_record(model_id, credential_id))
    session.add(_usage_event(team_a, model_id))
    session.add(_usage_event(team_b, model_id))
    session.add(_usage_event(team_b, model_id, cache_hit=False, cost=1.0))
    await session.commit()

    repo = SQLAlchemyUsageRepository(session)
    avoided, priced_hits, without_price, total = await repo.platform_cache_savings()
    assert priced_hits == 2
    assert without_price == 0
    assert total == 3
    assert avoided == pytest.approx(4e-5)
