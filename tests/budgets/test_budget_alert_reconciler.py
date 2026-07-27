"""Plan 07 Phase 2 — the budget-alert outbox worker's loop wiring.

`dispatch_pending` itself is covered by `test_budget_alert_dispatch.py`; this
covers the loop WIRING (create_task on startup, fail-safe try/except,
cancellation on shutdown), mirroring
`tests/misc/test_usage_outbox.py::test_reconciler_loop_ticks_survives_errors_and_shuts_down_cleanly`
exactly, since `make_budget_alert_dispatcher` is deliberately the same shape
as `make_usage_reconciler`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from litestar import Litestar
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import litestar_gateway.infrastructure.budget_alert_reconciler as reconciler_mod
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.persistence.database import create_database


async def test_dispatcher_loop_ticks_survives_errors_and_shuts_down_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'alerts_loop.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    database = create_database(settings)

    state = {"ticks": 0}
    reached = asyncio.Event()

    class FakeBudgetAlertStateRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def dispatch_pending(self, resolve_channels, *, limit: int) -> int:
            state["ticks"] += 1
            if state["ticks"] == 2:
                raise RuntimeError("transient dispatch failure")  # must not kill the loop
            if state["ticks"] >= 3:
                reached.set()
            return 0

    monkeypatch.setattr(reconciler_mod, "_DISPATCH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        reconciler_mod, "SQLAlchemyBudgetAlertStateRepository", FakeBudgetAlertStateRepository
    )

    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = Litestar(route_handlers=[])
    app.state[database.config.session_maker_app_state_key] = maker

    lifespan = reconciler_mod.make_budget_alert_dispatcher(database, settings)
    async with lifespan(app):
        await asyncio.wait_for(reached.wait(), timeout=5)
    assert state["ticks"] >= 3
    await engine.dispose()


async def test_dispatcher_constructs_and_tears_down_cleanly(
    tmp_path: Path,
) -> None:
    """The worker is fully decoupled from any request: it must construct and
    tear down without raising even when no channel is configured (every alert
    resolves to nothing to deliver to)."""
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'alerts_loop2.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    database = create_database(settings)
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = Litestar(route_handlers=[])
    app.state[database.config.session_maker_app_state_key] = maker

    lifespan = reconciler_mod.make_budget_alert_dispatcher(database, settings)
    async with lifespan(app):
        pass  # must construct + tear down without raising
    await engine.dispose()
