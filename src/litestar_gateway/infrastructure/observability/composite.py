"""Fan a TraceRecord out to several sinks, isolating their failures."""

from __future__ import annotations

import logging

from litestar_gateway.domain.entities import TraceRecord
from litestar_gateway.domain.ports import TraceSink

logger = logging.getLogger("litestar_gateway.observability")


class CompositeTraceSink:
    """One sink failing (e.g. MLflow down) must not starve the others."""

    def __init__(self, sinks: list[TraceSink]) -> None:
        self._sinks = sinks

    def write(self, record: TraceRecord) -> None:
        for sink in self._sinks:
            try:
                sink.write(record)
            except Exception:
                logger.warning("trace sink %s failed", type(sink).__name__, exc_info=True)
