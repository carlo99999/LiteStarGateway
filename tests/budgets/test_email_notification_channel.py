"""Plan 07 Phase 3 — email `NotificationChannel` adapter.

Uses a fake SMTP transport injected via `email_module._smtp_factory` (the same
module-level-seam pattern the webhook channel tests use for
`webhook_module._client_factory`), so no real network/SMTP I/O happens: we
assert the recipient/subject/body content and that transport failures propagate
out of `send` rather than being swallowed."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from uuid import uuid4

import pytest

import litestar_gateway.infrastructure.notifications.email_channel as email_module
from litestar_gateway.domain.entities import BudgetWindow, PendingBudgetAlert
from litestar_gateway.domain.money import to_cost
from litestar_gateway.infrastructure.notifications.email_channel import (
    EmailNotificationChannel,
)


def _alert(**overrides) -> PendingBudgetAlert:
    defaults = dict(
        id=uuid4(),
        team_id=uuid4(),
        window=BudgetWindow.MONTHLY,
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        threshold=80,
        spend=to_cost("85.0"),
        limit_cost=to_cost("100.0"),
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PendingBudgetAlert(**defaults)


class _FakeSMTP:
    """Records the calls an `EmailNotificationChannel._deliver` makes."""

    def __init__(self, host: str, port: int, log: dict) -> None:
        log["host"] = host
        log["port"] = port
        log.setdefault("starttls", False)
        log.setdefault("login", None)
        self._log = log

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def starttls(self) -> None:
        self._log["starttls"] = True

    def login(self, username: str, password: str) -> None:
        self._log["login"] = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        self._log["message"] = message


def _patch_smtp(monkeypatch: pytest.MonkeyPatch, log: dict) -> None:
    monkeypatch.setattr(
        email_module, "_smtp_factory", lambda host, port: _FakeSMTP(host, port, log)
    )


def _channel(**overrides) -> EmailNotificationChannel:
    defaults = dict(
        host="smtp.example.com",
        port=587,
        username="mailer",
        password="secret",  # pragma: allowlist secret
        use_tls=True,
        from_address="alerts@example.com",
        recipient="team@example.com",
    )
    defaults.update(overrides)
    return EmailNotificationChannel(**defaults)


# ── unit: well-formed message ────────────────────────────────────────────────


async def test_send_builds_and_delivers_message(monkeypatch: pytest.MonkeyPatch) -> None:
    log: dict = {}
    _patch_smtp(monkeypatch, log)
    alert = _alert()

    await _channel().send(alert)

    assert log["host"] == "smtp.example.com"
    assert log["port"] == 587
    assert log["starttls"] is True
    assert log["login"] == ("mailer", "secret")  # pragma: allowlist secret
    message: EmailMessage = log["message"]
    assert message["From"] == "alerts@example.com"
    assert message["To"] == "team@example.com"
    assert "80%" in message["Subject"]
    body = message.get_content()
    assert str(alert.team_id) in body
    assert "monthly" in body
    assert "80%" in body
    assert "85.00 USD" in body
    assert "100.00 USD" in body


async def test_no_tls_skips_starttls_and_no_username_skips_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: dict = {}
    _patch_smtp(monkeypatch, log)

    await _channel(use_tls=False, username=None, password=None).send(_alert())

    assert log["starttls"] is False
    assert log["login"] is None


async def test_smtp_failure_propagates_out_of_send(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenSMTP:
        def __init__(self, *a) -> None: ...
        def __enter__(self) -> _BrokenSMTP:
            return self

        def __exit__(self, *exc) -> None:
            return None

        def starttls(self) -> None: ...
        def login(self, *a) -> None: ...
        def send_message(self, message: object) -> None:
            raise RuntimeError("smtp down")

    monkeypatch.setattr(email_module, "_smtp_factory", lambda host, port: _BrokenSMTP())
    with pytest.raises(RuntimeError, match="smtp down"):
        await _channel().send(_alert())


def test_rejects_empty_required_fields() -> None:
    for bad in (
        dict(host=""),
        dict(from_address=""),
        dict(recipient=""),
    ):
        with pytest.raises(ValueError):
            _channel(**bad)
