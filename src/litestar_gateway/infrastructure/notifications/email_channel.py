"""Email `NotificationChannel` adapter (Plan 07 Phase 3, design doc §4).

A second delivery channel alongside `webhook_channel.py`, implementing the
same abstract `NotificationChannel` Protocol. SMTP is entirely an
infrastructure concern: the port stays abstract, and nothing here (smtplib,
credentials, the From/recipient addresses) leaks into `domain`/`application`.

Uses the standard library (`smtplib` + `email.message.EmailMessage`) rather
than a new dependency — v1 needs a plain STARTTLS/plain submission, not a
provider SDK. The blocking send runs in a worker thread via
`asyncio.to_thread` so it never blocks the outbox worker's event loop.

Each instance owns its own recipient (per the `NotificationChannel` design —
`send` takes only the alert), so the outbox worker constructs one channel per
team-configured recipient at dispatch time via the channel resolver."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from litestar_gateway.domain.entities import PendingBudgetAlert


def _smtp_factory(host: str, port: int) -> smtplib.SMTP:
    """Open an SMTP connection (module-level for test injection, mirroring
    `application/routing/webhook.py`'s `_client_factory`). Tests monkeypatch
    this to return a fake transport so no real network I/O happens."""
    return smtplib.SMTP(host, port, timeout=10)


class EmailNotificationChannel:
    """Delivers a fired budget-threshold alert as a plaintext email to one
    recipient via the platform SMTP server. Raises on any SMTP failure — the
    outbox worker catches it, records the attempt, and retries later (the
    channel never swallows failures itself, per the port contract)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        use_tls: bool,
        from_address: str,
        recipient: str,
    ) -> None:
        if not host:
            raise ValueError("email channel requires an SMTP host")
        if not from_address:
            raise ValueError("email channel requires a from_address")
        if not recipient:
            raise ValueError("email channel requires a recipient")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._from_address = from_address
        self._recipient = recipient

    def _build_message(self, alert: PendingBudgetAlert) -> EmailMessage:
        pct = alert.threshold
        message = EmailMessage()
        message["Subject"] = f"Budget alert: {pct}% of the {alert.window.value} cap reached"
        message["From"] = self._from_address
        message["To"] = self._recipient
        message.set_content(
            "A team's spend has crossed a configured budget threshold.\n\n"
            f"Team:          {alert.team_id}\n"
            f"Window:        {alert.window.value}\n"
            f"Period start:  {alert.period_start.isoformat()}\n"
            f"Threshold:     {pct}%\n"
            f"Spend:         {alert.spend:.2f} USD\n"
            f"Limit:         {alert.limit_cost:.2f} USD\n"
        )
        return message

    def _deliver(self, message: EmailMessage) -> None:
        """Blocking SMTP send, run in a worker thread by `send`."""
        with _smtp_factory(self._host, self._port) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password or "")
            smtp.send_message(message)

    async def send(self, alert: PendingBudgetAlert) -> None:
        await asyncio.to_thread(self._deliver, self._build_message(alert))
