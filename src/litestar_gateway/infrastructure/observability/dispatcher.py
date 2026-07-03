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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from litestar import Litestar

from litestar_test.domain.entities import TraceRecord
from litestar_test.domain.ports import TraceSink

logger = logging.getLogger("litestar_test.observability")


class TraceDispatcher:
    def __init__(self, sink: TraceSink, maxsize: int = 2000) -> None:
        self._sink = sink
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=maxsize)

    def enqueue(self, record: TraceRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning("trace queue full; dropping trace")

    async def _worker(self) -> None:
        while True:
            record = await self._queue.get()
            try:
                await anyio.to_thread.run_sync(self._sink.write, record)
            except Exception:  # a sink error must never take down the worker
                logger.warning("failed to write trace", exc_info=True)
            finally:
                self._queue.task_done()

    @asynccontextmanager
    async def run(self, _: Litestar) -> AsyncIterator[None]:
        task = asyncio.create_task(self._worker())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
