"""R7-M51: fire-and-forget shadow tasks must be drained on shutdown.

The shadow-routing tasks are `asyncio.create_task(...)` with only a strong ref
in `_SHADOW_TASKS` to survive GC. Without a shutdown drain they race the
SQLAlchemy plugin disposing the engine, and their unit of work (shadow
decision/usage rows) is silently lost. `drain_shadow_tasks` awaits them with a
bounded timeout, cancelling any that overrun, and the app registers it to run
before the DB engine is disposed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from litestar_gateway.app import _shadow_drain_lifespan, create_app
from litestar_gateway.application.routing import service
from litestar_gateway.application.routing.service import _SHADOW_TASKS, drain_shadow_tasks
from litestar_gateway.config import Settings


@pytest.fixture(autouse=True)
def _clear_shadow_tasks() -> AsyncIterator[None]:
    _SHADOW_TASKS.clear()
    yield
    _SHADOW_TASKS.clear()


def _register(coro) -> asyncio.Task:
    """Mirror how RouterService.route registers a shadow task."""
    task = asyncio.ensure_future(coro)
    _SHADOW_TASKS.add(task)
    task.add_done_callback(_SHADOW_TASKS.discard)
    return task


async def test_drain_awaits_pending_task_to_completion() -> None:
    ran = asyncio.Event()

    async def shadow() -> None:
        await asyncio.sleep(0.02)
        ran.set()

    task = _register(shadow())
    assert not task.done()

    await drain_shadow_tasks(timeout=5)

    assert task.done()
    assert ran.is_set()
    assert not _SHADOW_TASKS


async def test_drain_cancels_task_that_overruns_timeout() -> None:
    async def stuck() -> None:
        await asyncio.Event().wait()  # never completes on its own

    task = _register(stuck())

    await drain_shadow_tasks(timeout=0.05)

    assert task.cancelled()
    assert not _SHADOW_TASKS


async def test_drain_is_a_noop_without_tasks() -> None:
    await drain_shadow_tasks(timeout=5)
    assert not _SHADOW_TASKS


def test_app_registers_drain_to_run_before_engine_disposal(tmp_path: Path) -> None:
    # The drain must be the LAST lifespan manager so it unwinds first (LIFO),
    # i.e. before the SQLAlchemy plugin's engine-disposal lifespan (appended
    # during on_app_init). Registering it after construction guarantees this.
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'drain.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    app = create_app(settings)
    assert app._lifespan_managers[-1] is _shadow_drain_lifespan


async def test_drain_lifespan_cancels_in_flight_task_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A short drain budget keeps the cancel path fast and deterministic.
    monkeypatch.setattr(service, "SHADOW_DRAIN_TIMEOUT_S", 0.05)

    async def stuck() -> None:
        await asyncio.Event().wait()

    async with _shadow_drain_lifespan(None):  # type: ignore[arg-type]  # app unused
        task = _register(stuck())
        assert not task.done()

    # Exiting the lifespan (shutdown) drained the in-flight task: it was
    # cancelled rather than abandoned, and the registry is clear.
    assert task.cancelled()
    assert not _SHADOW_TASKS
