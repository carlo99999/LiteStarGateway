"""Logging factory: pick the logger type by environment.

Development gets human-readable console logs (stdlib `LoggingConfig`); production
gets structured **JSON** logs (structlog) suited to log aggregators. Both log
exceptions server-side; the app never runs with `debug=True`, so 5xx responses
stay generic (no stack traces leak to clients).
"""

from __future__ import annotations

from litestar.logging import LoggingConfig, StructLoggingConfig
from litestar.logging.config import BaseLoggingConfig, default_structlog_processors

from litestar_gateway.config import Settings

# Routine client noise, not app bugs: unknown paths (browser/scanner probes like
# Chrome's /.well-known/appspecific/com.chrome.devtools.json) and wrong methods
# would otherwise be logged as ERROR with a full traceback on every hit. The
# access-log line (404/405) still records the request; real errors (5xx) and
# every other status keep their stack traces.
_QUIET_STATUSES: set[int | type[Exception]] = {404, 405}


def build_logging_config(settings: Settings) -> BaseLoggingConfig:
    if settings.is_production:
        # Structured JSON — one object per line, parseable by log pipelines.
        return StructLoggingConfig(
            processors=default_structlog_processors(as_json=True),
            log_exceptions="always",
            disable_stack_trace=_QUIET_STATUSES,
        )
    # Human-readable console output for local development.
    return LoggingConfig(log_exceptions="always", disable_stack_trace=_QUIET_STATUSES)
