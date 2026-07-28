"""Money survives the database unchanged — asserted, not assumed.

The plan for this migration called out the risk explicitly: PostgreSQL has a
native NUMERIC, SQLite does not, and SQLAlchemy stores the value as a float
there. So the round-trip is a test on both dialects rather than a belief about
one. `tests/conftest.py` hands out a PostgreSQL database when DATABASE_URL points
at one (the CI job) and a SQLite file otherwise, so this file runs against both
without knowing which.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import UsageEvent
from litestar_gateway.domain.money import to_cost
from litestar_gateway.infrastructure.persistence.orm import OrganizationModel, TeamModel
from litestar_gateway.infrastructure.persistence.usage_repository import (
    SQLAlchemyUsageRepository,
)

# Amounts a binary float gets wrong. 0.1 + 0.2 is the canonical one; the rest
# are ordinary prices that are not representable in base 2.
AWKWARD = ["0.1", "0.2", "0.3", "0.07", "0.29", "1.005", "12.345678", "0.000001"]


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _team(session: AsyncSession):
    """A real team row: PostgreSQL enforces `usage_event.team_id`'s foreign key
    where SQLite (without PRAGMA foreign_keys) does not."""
    organization = OrganizationModel(id=uuid4(), name=f"org-{uuid4().hex[:8]}")
    team = TeamModel(id=uuid4(), name=f"team-{uuid4().hex[:8]}", organization_id=organization.id)
    session.add(organization)
    session.add(team)
    await session.commit()
    return team.id


def _event(team_id, cost: Decimal, at: datetime) -> UsageEvent:
    return UsageEvent(
        id=uuid4(),
        team_id=team_id,
        api_key_id=None,
        model_id=uuid4(),
        model_name="m",
        operation="chat.completions",
        prompt_tokens=1,
        completion_tokens=1,
        cost=cost,
        created_at=at,
    )


@pytest.mark.parametrize("amount", AWKWARD)
async def test_a_cost_round_trips_exactly(session: AsyncSession, amount: str) -> None:
    repo = SQLAlchemyUsageRepository(session)
    team_id = await _team(session)
    now = datetime.now(UTC)
    await repo.record(_event(team_id, Decimal(amount), now))

    total = await repo.spend_since(team_id, now.replace(hour=0, minute=0, second=0))

    assert total == to_cost(amount)


async def test_summing_amounts_floats_cannot_represent_is_exact(session: AsyncSession) -> None:
    """0.1 + 0.2 == 0.30000000000000004 in binary floats. Summed in the database
    across three rows, the ledger must still say 0.60 exactly — this is the
    number the budget gate compares against the cap."""
    repo = SQLAlchemyUsageRepository(session)
    team_id = await _team(session)
    now = datetime.now(UTC)
    for amount in ("0.1", "0.2", "0.3"):
        await repo.record(_event(team_id, Decimal(amount), now))

    total = await repo.spend_since(team_id, now.replace(hour=0, minute=0, second=0))

    assert total == to_cost("0.6")


async def test_the_total_does_not_depend_on_insertion_order(session: AsyncSession) -> None:
    repo = SQLAlchemyUsageRepository(session)
    now = datetime.now(UTC)
    since = now.replace(hour=0, minute=0, second=0)
    amounts = [Decimal(a) for a in AWKWARD]

    forward, backward = await _team(session), await _team(session)
    for amount in amounts:
        await repo.record(_event(forward, amount, now))
    for amount in reversed(amounts):
        await repo.record(_event(backward, amount, now))

    assert await repo.spend_since(forward, since) == await repo.spend_since(backward, since)
