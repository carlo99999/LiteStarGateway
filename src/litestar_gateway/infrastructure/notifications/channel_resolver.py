"""Per-alert channel resolution for the budget-alert outbox (Plan 07 Phase 3).

The outbox worker no longer dispatches through a fixed platform-wide channel
list: delivery targets are per-team data on the team's `Budget`. For each
pending alert this resolver reads the owning team's budget and builds the
channel instance(s) for THAT team:

- **Webhook** — the team's own `alert_webhook_url` if set, otherwise the
  platform-wide `Settings.budget_alert_webhook_url` (Phase 2's target, so teams
  without an override keep delivering there — backward compatible). The
  platform bearer token is applied ONLY to the platform URL, never to a
  team-supplied URL (a team's endpoint must not receive the platform's token).
- **Email** — the team's `alert_email` recipient via the platform SMTP server,
  but only when SMTP is actually configured; otherwise the recipient is ignored.

If a team configures BOTH, both channels are returned and BOTH fire for the
same alert (the outbox worker loops over the whole returned sequence). A team
with neither an override, no platform webhook, and no SMTP resolves to an empty
sequence — the worker leaves that row queued (a no-op, not a failure)."""

from __future__ import annotations

from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import PendingBudgetAlert
from litestar_gateway.domain.ports import BudgetRepository
from litestar_gateway.domain.ports.notification_channel import (
    ChannelResolver,
    NotificationChannel,
)
from litestar_gateway.infrastructure.notifications.email_channel import (
    EmailNotificationChannel,
)
from litestar_gateway.infrastructure.notifications.webhook_channel import (
    WebhookNotificationChannel,
)


def make_channel_resolver(budgets: BudgetRepository, settings: Settings) -> ChannelResolver:
    """Build the per-alert channel resolver bound to a budget-lookup port and
    the platform settings. `budgets` must share the same session/unit-of-work
    the outbox drain runs in (see `budget_alert_reconciler.py`)."""

    async def resolve(alert: PendingBudgetAlert) -> list[NotificationChannel]:
        budget = await budgets.get(alert.team_id)
        channels: list[NotificationChannel] = []

        team_webhook = budget.alert_webhook_url if budget else None
        if team_webhook:
            channels.append(
                WebhookNotificationChannel(
                    team_webhook, timeout_ms=settings.budget_alert_webhook_timeout_ms
                )
            )
        elif settings.budget_alert_webhook_url:
            channels.append(
                WebhookNotificationChannel(
                    settings.budget_alert_webhook_url,
                    bearer_token=settings.budget_alert_webhook_bearer_token,
                    timeout_ms=settings.budget_alert_webhook_timeout_ms,
                )
            )

        team_email = budget.alert_email if budget else None
        if team_email and settings.smtp_configured:
            channels.append(
                EmailNotificationChannel(
                    host=settings.smtp_host,  # type: ignore[arg-type]  # smtp_configured guards None
                    port=settings.smtp_port,
                    username=settings.smtp_username,
                    password=settings.smtp_password,
                    use_tls=settings.smtp_use_tls,
                    from_address=settings.smtp_from_address,  # type: ignore[arg-type]
                    recipient=team_email,
                )
            )

        return channels

    return resolve
