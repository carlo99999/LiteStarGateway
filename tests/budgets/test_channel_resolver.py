"""Plan 07 Phase 3 — per-alert channel resolution.

`make_channel_resolver` maps each alert to the delivery channel(s) configured
on its owning team's budget, falling back to the platform-wide webhook and
using the platform SMTP server for email. These tests drive it with a fake
budget repository and real `Settings`, asserting which concrete channel
instances come back for each config combination."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Budget, BudgetWindow, PendingBudgetAlert
from litestar_gateway.infrastructure.notifications.channel_resolver import make_channel_resolver
from litestar_gateway.infrastructure.notifications.email_channel import EmailNotificationChannel
from litestar_gateway.infrastructure.notifications.webhook_channel import (
    WebhookNotificationChannel,
)

TEAM = uuid4()


class _FakeBudgets:
    """A read-only `BudgetRepository` stub (only `get` is exercised; `set`/
    `remove` exist to satisfy the protocol for type checking)."""

    def __init__(self, budget: Budget | None) -> None:
        self._budget = budget

    async def get(self, team_id: UUID) -> Budget | None:
        return self._budget

    async def set(self, budget: Budget) -> Budget:  # pragma: no cover - unused
        raise NotImplementedError

    async def remove(self, team_id: UUID) -> None:  # pragma: no cover - unused
        raise NotImplementedError


def _budget(**overrides) -> Budget:
    defaults = dict(
        id=uuid4(),
        team_id=TEAM,
        limit_cost=100.0,
        window=BudgetWindow.MONTHLY,
        created_at=datetime.now(UTC),
        thresholds=[50],
    )
    defaults.update(overrides)
    return Budget(**defaults)


def _alert() -> PendingBudgetAlert:
    return PendingBudgetAlert(
        id=uuid4(),
        team_id=TEAM,
        window=BudgetWindow.MONTHLY,
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        threshold=50,
        spend=50.0,
        limit_cost=100.0,
        created_at=datetime.now(UTC),
    )


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_url="sqlite+aiosqlite:///:memory:",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    defaults.update(overrides)
    return Settings(**defaults)


_SMTP = dict(
    smtp_host="smtp.example.com",
    smtp_from_address="alerts@example.com",
)


async def test_both_channels_when_team_configures_webhook_and_email() -> None:
    budget = _budget(
        alert_webhook_url="https://team.example.com/hook", alert_email="team@example.com"
    )
    resolve = make_channel_resolver(_FakeBudgets(budget), _settings(**_SMTP))

    channels = await resolve(_alert())

    assert [type(c) for c in channels] == [WebhookNotificationChannel, EmailNotificationChannel]


async def test_email_ignored_when_smtp_not_configured() -> None:
    budget = _budget(alert_email="team@example.com")
    resolve = make_channel_resolver(_FakeBudgets(budget), _settings())  # no SMTP

    assert await resolve(_alert()) == []


async def test_email_only_when_smtp_configured() -> None:
    budget = _budget(alert_email="team@example.com")
    resolve = make_channel_resolver(_FakeBudgets(budget), _settings(**_SMTP))

    channels = await resolve(_alert())
    assert [type(c) for c in channels] == [EmailNotificationChannel]


async def test_team_webhook_overrides_platform_webhook() -> None:
    budget = _budget(alert_webhook_url="https://team.example.com/hook")
    resolve = make_channel_resolver(
        _FakeBudgets(budget),
        _settings(budget_alert_webhook_url="https://platform.example.com/hook"),
    )

    channels = await resolve(_alert())
    assert len(channels) == 1
    assert isinstance(channels[0], WebhookNotificationChannel)
    assert str(channels[0]._url) == "https://team.example.com/hook"


async def test_falls_back_to_platform_webhook_without_team_override() -> None:
    budget = _budget()  # no per-team webhook
    resolve = make_channel_resolver(
        _FakeBudgets(budget),
        _settings(budget_alert_webhook_url="https://platform.example.com/hook"),
    )

    channels = await resolve(_alert())
    assert len(channels) == 1
    assert isinstance(channels[0], WebhookNotificationChannel)
    assert str(channels[0]._url) == "https://platform.example.com/hook"


async def test_no_budget_falls_back_to_platform_webhook() -> None:
    resolve = make_channel_resolver(
        _FakeBudgets(None),
        _settings(budget_alert_webhook_url="https://platform.example.com/hook"),
    )

    channels = await resolve(_alert())
    assert len(channels) == 1
    assert isinstance(channels[0], WebhookNotificationChannel)
    assert str(channels[0]._url) == "https://platform.example.com/hook"


async def test_nothing_configured_resolves_to_empty() -> None:
    resolve = make_channel_resolver(_FakeBudgets(_budget()), _settings())
    assert await resolve(_alert()) == []
