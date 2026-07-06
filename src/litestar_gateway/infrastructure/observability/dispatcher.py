"""Off-hot-path trace dispatch: a bounded queue drained by a background worker.

Request handlers `enqueue` a `TraceRecord` (non-blocking; dropped with a warning
if the queue is full — backpressure). A single worker drains the queue and calls
the sink in a thread (the MLflow client is blocking). Failures are logged, never
propagated. Started/stopped via the app lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import anyio.to_thread as to_thread
from litestar import Litestar

from litestar_gateway.domain.entities import TraceRecord
from litestar_gateway.domain.ports import TraceSink

logger = logging.getLogger("litestar_gateway.observability")


class TraceDispatcher:
    def __init__(
        self,
        sink: TraceSink,
        maxsize: int = 2000,
        on_drop: Callable[[], None] | None = None,
    ) -> None:
        self._sink = sink
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=maxsize)
        self._on_drop = on_drop

    def enqueue(self, record: TraceRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            # Observable, not just logged: on_drop feeds the dropped-traces
            # metric so silent trace loss shows up on a dashboard.
            if self._on_drop is not None:
                self._on_drop()
            logger.warning("trace queue full; dropping trace")

    async def _worker(self) -> None:
        while True:
            record = await self._queue.get()
            try:
                await to_thread.run_sync(self._sink.write, record)
            except Exception:  # a sink error must never take down the worker
                logger.warning("failed to write trace", exc_info=True)
            finally:
                self._queue.task_done()

    @asynccontextmanager
    async def run(self, _: Litestar) -> AsyncGenerator[None]:
        task = asyncio.create_task(self._worker())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
