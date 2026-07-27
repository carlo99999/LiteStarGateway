"""Request correlation id: generate/accept, bind to structlog, echo in response.

Every HTTP request is tagged with an opaque id (`X-Request-ID`) that structlog
binds into every log line emitted while handling it, and that the response
echoes back to the caller. The same id is threaded into the `TraceRecord`,
`UsageEvent`, `AuditEvent` and `RoutingDecisionRecord` created for that request
(via `current_request_id()`, read from structlog's contextvars at the point
each is constructed) so one inference can be followed end-to-end across logs,
traces, billing and the audit trail (docs/logging.md §2).

Trust model: an inbound `X-Request-ID` is accepted verbatim only when the
direct connecting peer is a configured trusted proxy (`Settings.trusted_proxy_ips`,
mirroring `FORWARDED_ALLOW_IPS` at the ASGI-server layer — see docs/operations.md)
AND the value passes length/charset validation. Anything else (untrusted
source, malformed value, or no inbound header) gets a freshly generated id —
an inbound value is never trusted verbatim from an arbitrary caller, which
would let a client inject arbitrary content into structured log lines.
"""

from __future__ import annotations

import re
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware

from litestar_gateway.request_context import current_request_id

if TYPE_CHECKING:
    from litestar.types import ASGIApp, Message, Receive, Scope, Send

    from litestar_gateway.config import Settings

__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIDMiddleware",
    "current_request_id",
    "resolve_request_id",
]

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES = b"x-request-id"

# Opaque token only: letters/digits/hyphen/underscore, 1-128 chars — comfortably
# covers a UUID4 (36 chars) or similar client-generated correlation ids while
# rejecting anything that could carry control characters or absurd length into
# a structured log line.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _is_trusted_proxy(client_ip: str | None, settings: Settings) -> bool:
    if not client_ip or not settings.trusted_proxy_ips:
        return False
    try:
        addr = ip_address(client_ip)
    except ValueError:
        return False
    for cidr in settings.trusted_proxy_ips:
        try:
            network = ip_network(cidr, strict=False)
        except ValueError:
            continue
        if addr in network:
            return True
    return False


def resolve_request_id(inbound: str | None, client_ip: str | None, settings: Settings) -> str:
    """Pick the request id for one request.

    An inbound value is trusted verbatim only from a configured trusted proxy
    AND only when it passes length/charset validation; every other case
    (untrusted source, invalid value, or no inbound header at all) generates a
    fresh id rather than trusting client input into the log/trace pipeline.
    """
    if inbound and _VALID_REQUEST_ID.match(inbound) and _is_trusted_proxy(client_ip, settings):
        return inbound
    return uuid4().hex


class RequestIDMiddleware(ASGIMiddleware):
    """Resolve/bind/echo the request id for every HTTP request.

    Binding happens via `structlog.contextvars.bound_contextvars`, active for
    the whole downstream call — including Litestar's own exception-handling
    middleware, so a 500 response is tagged and logged with the same id as
    every other response. The header is injected at the ASGI `send` layer so
    it is present on every response, generic 500s included.
    """

    scopes = (ScopeType.HTTP,)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        headers = dict(scope.get("headers") or ())
        inbound_raw = headers.get(_REQUEST_ID_HEADER_BYTES)
        inbound = inbound_raw.decode("latin-1") if inbound_raw is not None else None
        client = scope.get("client")
        client_ip = client[0] if client else None
        request_id = resolve_request_id(inbound, client_ip, self._settings)
        header_bytes = (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), header_bytes]}  # type: ignore[typeddict-item]
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            await next_app(scope, receive, send_wrapper)
