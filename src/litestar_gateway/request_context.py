"""Request correlation id: read-only accessor for application/domain code.

The value is bound per-request by
`infrastructure.web.request_id.RequestIDMiddleware` via `structlog.contextvars`.
This module holds only the read side, in a location application-layer code can
import without reaching into `infrastructure` or `litestar` (the hexagonal
boundary domain/ and application/ must respect): `structlog` here is a plain
contextvar-based logging library, not part of the litestar/sqlalchemy/
infrastructure stack that boundary excludes — the same way `application/`
already uses stdlib `logging` directly.

Used to tag `TraceRecord`, `UsageEvent`, `AuditEvent` and `RoutingDecisionRecord`
with the request's correlation id at the point each is constructed (Plan 11
Slice A, docs/logging.md §2), without threading an extra parameter through
every intervening method signature.
"""

from __future__ import annotations

import structlog


def current_request_id() -> str | None:
    """The request id bound for the in-flight request, or `None` outside a
    request context (e.g. a background reconciler with no request to tag)."""
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None
