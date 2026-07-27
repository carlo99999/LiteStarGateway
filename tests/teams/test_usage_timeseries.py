"""Plan 10 Phase 1: `SQLAlchemyUsageRepository.timeseries` + the
`/teams/{id}/usage/timeseries` endpoint.

Table-driven repository tests run against SQLite and Postgres (via the root
`database_url` fixture, mirroring `tests/completions/test_cache_savings.py`'s
structure for the analogous cache-savings aggregate): bucket boundaries and
totals must be IDENTICAL across both dialects for the same seeded data. HTTP
tests cover RBAC (team isolation, `usage:read`), filters and validation.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.infrastructure.llm import openai_adapter
from litestar_gateway.infrastructure.persistence.orm import (
    APIKeyModel,
    CredentialModel,
    OrganizationModel,
    SecretKeyModel,
    TeamModel,
    UsageEventModel,
    UserModel,
)
from litestar_gateway.infrastructure.persistence.usage_repository import SQLAlchemyUsageRepository

from .conftest import _admin, _bearer, _team_and_credential


class _EchoClient:
    """Always returns the same usage — enough to prove a real ledger write
    reaches the timeseries endpoint, mirroring
    `tests/completions/test_cache_savings.py`'s `EchoClient`."""

    def __init__(self, **kwargs: object) -> None:
        self.chat = SimpleNamespace(completions=self)

    async def close(self) -> None:
        return None

    async def create(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            model_dump=lambda: {
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
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }
        )

# --- Repository-level table-driven tests ------------------------------------


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
    org = OrganizationModel(id=uuid4(), name=f"org-{uuid4()}")
    session.add(org)
    await session.flush()
    team = TeamModel(id=uuid4(), organization_id=org.id, name=f"team-{uuid4()}")
    session.add(team)
    await session.flush()
    return team.id


async def _seed_credential(session: AsyncSession) -> UUID:
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


async def _seed_api_key(session: AsyncSession, team_id: UUID) -> UUID:
    """A real `api_key` row: `usage_event.api_key_id` is a Postgres foreign
    key (SQLite doesn't enforce it locally, but the CI Postgres job does)."""
    user = UserModel(id=uuid4(), email=f"u-{uuid4()}@corp.com", password_hash="x")
    session.add(user)
    await session.flush()
    key = APIKeyModel(
        id=uuid4(),
        team_id=team_id,
        created_by=user.id,
        prefix="sk-test",
        key_hash=f"hash-{uuid4()}",
    )
    session.add(key)
    await session.flush()
    return key.id


def _usage_event(
    team_id: UUID, model_id: UUID, created_at: datetime, **overrides: object
) -> UsageEventModel:
    fields: dict[str, object] = {
        "team_id": team_id,
        "api_key_id": None,
        "model_id": model_id,
        "model_name": "m",
        "operation": "chat.completions",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost": 1.0,
        "created_at": created_at,
        **overrides,
    }
    return UsageEventModel(**fields)


async def test_timeseries_empty_range_returns_empty_series(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
        granularity="day",
    )
    assert buckets == []


async def test_timeseries_hour_granularity_groups_within_the_hour(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    model_id = uuid4()
    base_hour = datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)
    session.add(_usage_event(team_id, model_id, base_hour))
    session.add(_usage_event(team_id, model_id, base_hour + timedelta(minutes=45)))
    session.add(_usage_event(team_id, model_id, base_hour + timedelta(hours=1, minutes=5)))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id,
        start=base_hour,
        end=base_hour + timedelta(hours=2),
        granularity="hour",
    )
    assert [b.bucket_start for b in buckets] == [
        base_hour,
        base_hour + timedelta(hours=1),
    ]
    assert buckets[0].request_count == 2
    assert buckets[0].prompt_tokens == 20
    assert buckets[0].completion_tokens == 10
    assert buckets[0].cost == pytest.approx(2.0)
    assert buckets[1].request_count == 1


async def test_timeseries_day_granularity_groups_across_the_day(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    model_id = uuid4()
    day = datetime(2026, 3, 1, tzinfo=UTC)
    session.add(_usage_event(team_id, model_id, day.replace(hour=1)))
    session.add(_usage_event(team_id, model_id, day.replace(hour=23, minute=59)))
    session.add(_usage_event(team_id, model_id, day + timedelta(days=1, hours=2)))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id,
        start=day,
        end=day + timedelta(days=2),
        granularity="day",
    )
    assert [b.bucket_start for b in buckets] == [day, day + timedelta(days=1)]
    assert buckets[0].request_count == 2
    assert buckets[1].request_count == 1


async def test_timeseries_bucket_start_is_utc_and_bounds_are_half_open(
    session: AsyncSession,
) -> None:
    team_id = await _seed_team(session)
    model_id = uuid4()
    day = datetime(2026, 3, 1, tzinfo=UTC)
    session.add(_usage_event(team_id, model_id, day))
    # Exactly at `end`: excluded ([start, end) is half-open).
    session.add(_usage_event(team_id, model_id, day + timedelta(days=1)))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id, start=day, end=day + timedelta(days=1), granularity="day"
    )
    assert len(buckets) == 1
    assert buckets[0].bucket_start == day
    assert buckets[0].bucket_start.tzinfo is not None
    assert buckets[0].bucket_start.utcoffset() == timedelta(0)


async def test_timeseries_model_filter_matches_alias_or_canonical(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    model_a, model_b = uuid4(), uuid4()
    day = datetime(2026, 3, 1, tzinfo=UTC)
    session.add(
        _usage_event(
            team_id,
            model_a,
            day,
            requested_alias="fast",
            canonical_model_name="gpt-4o-mini",
        )
    )
    session.add(
        _usage_event(
            team_id,
            model_b,
            day,
            requested_alias="smart",
            canonical_model_name="gpt-4o",
        )
    )
    await session.commit()

    repo = SQLAlchemyUsageRepository(session)
    by_alias = await repo.timeseries(
        team_id, start=day, end=day + timedelta(days=1), granularity="day", model_name="fast"
    )
    assert len(by_alias) == 1
    assert by_alias[0].request_count == 1

    by_canonical = await repo.timeseries(
        team_id,
        start=day,
        end=day + timedelta(days=1),
        granularity="day",
        model_name="gpt-4o",
    )
    assert len(by_canonical) == 1
    assert by_canonical[0].request_count == 1


async def test_timeseries_requested_alias_filter_is_exact(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    model_id = uuid4()
    day = datetime(2026, 3, 1, tzinfo=UTC)
    session.add(_usage_event(team_id, model_id, day, requested_alias="fast"))
    session.add(_usage_event(team_id, model_id, day, requested_alias="fast-v2"))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id,
        start=day,
        end=day + timedelta(days=1),
        granularity="day",
        requested_alias="fast",
    )
    assert len(buckets) == 1
    assert buckets[0].request_count == 1


async def test_timeseries_api_key_id_filter(session: AsyncSession) -> None:
    team_id = await _seed_team(session)
    model_id = uuid4()
    key_a, key_b = await _seed_api_key(session, team_id), await _seed_api_key(session, team_id)
    day = datetime(2026, 3, 1, tzinfo=UTC)
    session.add(_usage_event(team_id, model_id, day, api_key_id=key_a))
    session.add(_usage_event(team_id, model_id, day, api_key_id=key_b))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id, start=day, end=day + timedelta(days=1), granularity="day", api_key_id=key_a
    )
    assert len(buckets) == 1
    assert buckets[0].request_count == 1


async def test_timeseries_is_tenant_isolated(session: AsyncSession) -> None:
    team_a, team_b = await _seed_team(session), await _seed_team(session)
    model_id = uuid4()
    day = datetime(2026, 3, 1, tzinfo=UTC)
    session.add(_usage_event(team_a, model_id, day))
    session.add(_usage_event(team_b, model_id, day))
    session.add(_usage_event(team_b, model_id, day))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_a, start=day, end=day + timedelta(days=1), granularity="day"
    )
    assert len(buckets) == 1
    assert buckets[0].request_count == 1


async def test_timeseries_is_dst_independent_across_a_us_dst_transition(
    session: AsyncSession,
) -> None:
    """2026-03-08 is a US spring-forward DST date (a local clock skips
    01:59:59 -> 03:00:00), but bucketing happens on UTC instants only. Hourly
    buckets over a range spanning that date must stay exactly one hour apart
    with no gap or duplicate, regardless of what any local calendar would do."""
    team_id = await _seed_team(session)
    model_id = uuid4()
    range_start = datetime(2026, 3, 8, 0, 0, tzinfo=UTC)
    hours = 10
    for i in range(hours):
        session.add(_usage_event(team_id, model_id, range_start + timedelta(hours=i)))
    await session.commit()

    buckets = await SQLAlchemyUsageRepository(session).timeseries(
        team_id,
        start=range_start,
        end=range_start + timedelta(hours=hours),
        granularity="hour",
    )
    assert len(buckets) == hours
    starts = [b.bucket_start for b in buckets]
    assert starts == [range_start + timedelta(hours=i) for i in range(hours)]
    for a, b in itertools.pairwise(starts):
        assert b - a == timedelta(hours=1)
    assert all(s.utcoffset() == timedelta(0) for s in starts)


# --- HTTP endpoint tests -----------------------------------------------------
#
# RBAC/team-isolation coverage (a billing-viewer of one team cannot read
# another team's timeseries) lives in tests/rbac/test_extended_team_roles.py,
# alongside the equivalent `/usage` (aggregate) coverage; these tests only
# cover the endpoint's own request/response contract (shape, filters,
# validation) using the platform admin, who can read every team.


async def test_usage_timeseries_returns_bucketed_data(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _cred = await _team_and_credential(client, admin, "ts")

    resp = await client.get(
        f"/teams/{team}/usage/timeseries"
        "?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z&granularity=day",
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["team_id"] == team
    assert body["granularity"] == "day"
    assert body["buckets"] == []


async def test_usage_timeseries_rejects_bad_granularity(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _cred = await _team_and_credential(client, admin, "bad-gran")

    resp = await client.get(
        f"/teams/{team}/usage/timeseries"
        "?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z&granularity=week",
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_usage_timeseries_rejects_inverted_range(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _cred = await _team_and_credential(client, admin, "bad-range")

    resp = await client.get(
        f"/teams/{team}/usage/timeseries"
        "?start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z&granularity=day",
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_usage_timeseries_reflects_a_real_call(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real `/v1/chat/completions` call lands in the same-day
    bucket with the exact prompt/completion counts the provider returned."""
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", _EchoClient)
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin, "e2e")
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "ts-model",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o-mini",
        },
        headers=_bearer(admin),
    )
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]

    chat = await client.post(
        "/v1/chat/completions",
        json={"model": "ts-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert chat.status_code == HTTP_200_OK, chat.text

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    resp = await client.get(
        f"/teams/{team}/usage/timeseries"
        f"?start={today.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={tomorrow.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        "&granularity=day",
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    buckets = resp.json()["buckets"]
    assert len(buckets) == 1
    assert buckets[0]["request_count"] == 1
    assert buckets[0]["prompt_tokens"] == 7
    assert buckets[0]["completion_tokens"] == 3
    assert buckets[0]["total_tokens"] == 10
