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


def build_logging_config(settings: Settings) -> BaseLoggingConfig:
    if settings.is_production:
        # Structured JSON — one object per line, parseable by log pipelines.
        return StructLoggingConfig(
            processors=default_structlog_processors(as_json=True),
            log_exceptions="always",
        )
    # Human-readable console output for local development.
    return LoggingConfig(log_exceptions="always")
