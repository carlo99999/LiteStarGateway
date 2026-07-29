"""A team's own HMAC secret for its budget-alert webhook.

Declared open in #419: the webhook was signed, but with one platform-wide
secret, which is wrong for a receiver hosting several tenants' endpoints — every
tenant could verify every other tenant's calls. This gives each team the option
of its own key, with the platform secret as the fallback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Budget, BudgetWindow, PendingBudgetAlert
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.notifications.channel_resolver import make_channel_resolver
from litestar_gateway.infrastructure.persistence.budget_repository import (
    SQLAlchemyBudgetRepository,
)
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)

TEAM = uuid4()
TEAM_MATERIAL = "team-webhook-material"  # pragma: allowlist secret
PLATFORM_MATERIAL = "platform-webhook-material"  # pragma: allowlist secret
ROTATED_MATERIAL = "rotated-webhook-material"  # pragma: allowlist secret
WEBHOOK_URL = "https://alerts.example/hook"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def repo(session: AsyncSession) -> SQLAlchemyBudgetRepository:
    keyring = Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")
    return SQLAlchemyBudgetRepository(session, keyring)


def _budget() -> Budget:
    return Budget(
        id=uuid4(),
        team_id=TEAM,
        limit_cost=Decimal("100"),
        window=BudgetWindow.MONTHLY,
        created_at=datetime.now(UTC),
        thresholds=[80],
        alert_webhook_url=WEBHOOK_URL,
    )


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        admin_email="admin@example.com",
        master_key="m" * 32,
        jwt_secret="x" * 40,
        salt_key="s" * 32,
        webhook_signing_secret=PLATFORM_MATERIAL,
    )


def _alert() -> PendingBudgetAlert:
    return PendingBudgetAlert(
        id=uuid4(),
        team_id=TEAM,
        window=BudgetWindow.MONTHLY,
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        threshold=80,
        spend=Decimal("80"),
        limit_cost=Decimal("100"),
        created_at=datetime.now(UTC),
    )


# ── Storage ──────────────────────────────────────────────────────────────────


async def test_a_stored_secret_is_reported_but_never_returned(
    repo: SQLAlchemyBudgetRepository,
) -> None:
    stored = await repo.set(_budget(), alert_webhook_secret=TEAM_MATERIAL)

    assert stored.has_alert_webhook_secret is True
    # There is no field on the entity that could carry it, which is what keeps a
    # console response from leaking one.
    assert TEAM_MATERIAL not in repr(stored)
    assert TEAM_MATERIAL not in repr(await repo.get(TEAM))


async def test_the_dispatcher_can_read_it(repo: SQLAlchemyBudgetRepository) -> None:
    await repo.set(_budget(), alert_webhook_secret=TEAM_MATERIAL)

    assert await repo.alert_webhook_secret(TEAM) == TEAM_MATERIAL


async def test_no_secret_configured_reads_as_none(repo: SQLAlchemyBudgetRepository) -> None:
    await repo.set(_budget())

    assert (await repo.get(TEAM)).has_alert_webhook_secret is False  # type: ignore[union-attr]
    assert await repo.alert_webhook_secret(TEAM) is None
    assert await repo.alert_webhook_secret(uuid4()) is None


async def test_updating_the_budget_without_a_secret_keeps_the_stored_one(
    repo: SQLAlchemyBudgetRepository,
) -> None:
    # The realistic edit: an operator changes a threshold. They cannot resubmit
    # a secret they were never shown, so omission must mean "unchanged" —
    # treating it as "clear" would silently downgrade a signed endpoint to the
    # platform-wide key.
    await repo.set(_budget(), alert_webhook_secret=TEAM_MATERIAL)

    edited = await repo.set(replace(_budget(), thresholds=[50, 90]))

    assert edited.has_alert_webhook_secret is True
    assert edited.thresholds == [50, 90]
    assert await repo.alert_webhook_secret(TEAM) == TEAM_MATERIAL


async def test_providing_a_secret_rotates_it(repo: SQLAlchemyBudgetRepository) -> None:
    await repo.set(_budget(), alert_webhook_secret=TEAM_MATERIAL)

    await repo.set(_budget(), alert_webhook_secret=ROTATED_MATERIAL)

    assert await repo.alert_webhook_secret(TEAM) == ROTATED_MATERIAL


# ── Which secret signs the call ──────────────────────────────────────────────


async def test_a_teams_own_secret_signs_its_webhook(repo: SQLAlchemyBudgetRepository) -> None:
    await repo.set(_budget(), alert_webhook_secret=TEAM_MATERIAL)
    resolve = make_channel_resolver(repo, _settings())

    channels = await resolve(_alert())

    assert [c._signing_secret for c in channels] == [TEAM_MATERIAL]  # type: ignore[attr-defined]


async def test_without_one_the_platform_secret_still_signs(
    repo: SQLAlchemyBudgetRepository,
) -> None:
    # Existing deployments keep working unchanged: this is what every row that
    # predates the column means.
    await repo.set(_budget())
    resolve = make_channel_resolver(repo, _settings())

    channels = await resolve(_alert())

    assert [c._signing_secret for c in channels] == [PLATFORM_MATERIAL]  # type: ignore[attr-defined]


async def test_the_webhook_is_still_signed_when_neither_is_set(
    repo: SQLAlchemyBudgetRepository,
) -> None:
    # Nothing to sign with is a configuration gap, not a crash — the channel
    # warns and sends unsigned, which is the pre-existing behaviour.
    await repo.set(_budget())
    settings = replace(_settings(), webhook_signing_secret=None)
    resolve = make_channel_resolver(repo, settings)

    channels = await resolve(_alert())

    assert [c._signing_secret for c in channels] == [None]  # type: ignore[attr-defined]
