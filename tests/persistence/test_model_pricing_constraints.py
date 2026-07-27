"""ISSUE-022, defense in depth: the DB refuses a negative rate too.

Application validation in `ModelService` is the primary gate, but the ledger is
money — a future write path (a script, a seeder, a repository added later) must
not be able to reintroduce a credit-producing rate. These insert the ORM row
directly, bypassing both the service and the repository, so what is under test
is the CHECK constraint itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import ModelType, Provider
from litestar_gateway.infrastructure.persistence.orm import ModelRecord

RATE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_write_cost_per_token",
    "cache_read_cost_per_token",
    "image_cost_per_image",
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pricing.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _row(**rates: float) -> ModelRecord:
    return ModelRecord(
        id=uuid4(),
        team_id=None,
        name=f"m-{uuid4().hex[:8]}",
        provider=Provider.OPENAI.value,
        credential_id=uuid4(),
        type=ModelType.CHAT.value,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        enabled=True,
        created_at=datetime.now(UTC),
        image_prices={},
        **rates,
    )


@pytest.mark.parametrize("field", RATE_FIELDS)
async def test_negative_rate_is_rejected_by_the_database(session: AsyncSession, field: str) -> None:
    session.add(_row(**{field: -1.0}))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_zero_and_positive_rates_are_accepted(session: AsyncSession) -> None:
    session.add(_row(input_cost_per_token=0.0, output_cost_per_token=0.000015))
    await session.commit()
