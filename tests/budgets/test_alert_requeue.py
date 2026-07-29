"""Replaying a quarantined budget alert.

The last unchecked box from the webhook production checklist (#419 shipped
signing, timestamps, ids and idempotency; replay was declared open). Two
properties matter: a quarantined alert is findable, and requeueing it cannot
disturb a row another dispatcher is currently working on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from litestar.status_codes import HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND
from litestar.testing import AsyncTestClient
from rbac.conftest import _admin, _bearer, _member_token, _team  # type: ignore[import-not-found]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import BudgetWindow
from litestar_gateway.infrastructure.persistence.budget_alert_state_repository import (
    MAX_DISPATCH_ATTEMPTS,
    SQLAlchemyBudgetAlertStateRepository,
)
from litestar_gateway.infrastructure.persistence.orm import PendingBudgetAlertModel

TEAM = uuid4()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alerts.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _row(
    session: AsyncSession,
    *,
    attempts: int,
    threshold: int = 80,
    claimed_until: datetime | None = None,
) -> PendingBudgetAlertModel:
    row = PendingBudgetAlertModel(
        id=uuid4(),
        team_id=TEAM,
        window=BudgetWindow.MONTHLY.value,
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        threshold=threshold,
        spend=Decimal("80"),
        limit_cost=Decimal("100"),
        attempts=attempts,
        last_error="ConnectError('unreachable')",
        claimed_until=claimed_until,
    )
    session.add(row)
    await session.commit()
    return row


async def test_only_quarantined_alerts_are_listed(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    await _row(session, attempts=0, threshold=50)
    await _row(session, attempts=MAX_DISPATCH_ATTEMPTS - 1, threshold=60)
    stuck = await _row(session, attempts=MAX_DISPATCH_ATTEMPTS, threshold=80)

    listed = await repo.quarantined_alerts()

    # A row still within its retry budget is not stuck — it is simply pending.
    assert [a.id for a in listed] == [stuck.id]


async def test_the_listing_explains_why_the_alert_is_stuck(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    await _row(session, attempts=MAX_DISPATCH_ATTEMPTS)

    alert = (await repo.quarantined_alerts())[0]

    assert alert.attempts == MAX_DISPATCH_ATTEMPTS
    # The only account of what went wrong; without it a replay is a guess.
    assert alert.last_error == "ConnectError('unreachable')"
    assert (alert.threshold, alert.spend, alert.limit_cost) == (80, Decimal("80"), Decimal("100"))


async def test_requeue_puts_a_quarantined_alert_back_in_the_queue(
    session: AsyncSession,
) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    stuck = await _row(session, attempts=MAX_DISPATCH_ATTEMPTS)

    assert await repo.requeue(stuck.id) is True

    assert await repo.quarantined_alerts() == []
    # Back in the drain's view, which skips quarantined rows.
    assert [a.id for a in await repo.pending_alerts()] == [stuck.id]


async def test_requeue_keeps_the_last_error(session: AsyncSession) -> None:
    # After a replay an operator still needs to know what failed the first ten
    # times — clearing it would erase the only record.
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    stuck = await _row(session, attempts=MAX_DISPATCH_ATTEMPTS)

    await repo.requeue(stuck.id)

    replayed = (await repo.pending_alerts())[0]
    assert replayed.last_error == "ConnectError('unreachable')"
    assert replayed.attempts == 0


async def test_requeue_clears_a_stale_lease(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    stuck = await _row(
        session,
        attempts=MAX_DISPATCH_ATTEMPTS,
        claimed_until=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert await repo.requeue(stuck.id) is True

    refreshed = await session.get(PendingBudgetAlertModel, stuck.id)
    assert refreshed is not None
    assert refreshed.claimed_until is None


async def test_requeueing_a_row_that_is_merely_retrying_does_nothing(
    session: AsyncSession,
) -> None:
    # The predicate is part of the UPDATE for this reason: clearing the lease of
    # a row a dispatcher is currently holding would hand a second dispatcher the
    # same alert to send.
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    claim = datetime.now(UTC) + timedelta(minutes=4)
    live = await _row(session, attempts=2, claimed_until=claim)

    assert await repo.requeue(live.id) is False

    refreshed = await session.get(PendingBudgetAlertModel, live.id)
    assert refreshed is not None
    assert refreshed.attempts == 2
    assert refreshed.claimed_until is not None


async def test_requeueing_an_unknown_id_is_false_not_an_error(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    assert await repo.requeue(uuid4()) is False


# ── The platform-admin endpoints ─────────────────────────────────────────────


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email="admin@example.com",
        master_key="master-secret",  # pragma: allowlist secret
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def test_the_endpoints_are_platform_admin_only(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "member@example.com", "admin")

    listed = await client.get("/platform/budget-alerts/quarantined", headers=_bearer(member))
    replayed = await client.post(
        f"/platform/budget-alerts/{uuid4()}/requeue", headers=_bearer(member)
    )

    # A *team* admin is not a platform admin: replaying sends a notification on
    # someone else's behalf, so it stays with the platform operator.
    assert listed.status_code == HTTP_403_FORBIDDEN
    assert replayed.status_code == HTTP_403_FORBIDDEN


async def test_replaying_an_unknown_alert_is_a_404(client: AsyncTestClient) -> None:
    admin = await _admin(client)

    response = await client.post(
        f"/platform/budget-alerts/{uuid4()}/requeue", headers=_bearer(admin)
    )

    assert response.status_code == HTTP_404_NOT_FOUND
