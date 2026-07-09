"""Anthropic-native Messages endpoint.

Authenticated by a team API key (`Authorization: Bearer lsk_...`), so a customer
can point the stock Anthropic client at our base URL:

    client = Anthropic(api_key="lsk_...", base_url="https://.../")
    client.messages.create(model="<team-model-alias>", messages=[...])

`request.user` is the team id (set by the API-key middleware); the request's
`model` is the team's model alias. This handler is registered on the protected
`api_router`, so the per-IP rate limit and API-key auth applied to
`/v1/chat/completions` guard it unchanged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from litestar import Request, Response, post
from litestar.di import NamedDependency
from litestar.response import ServerSentEvent
from litestar.response.sse import ServerSentEventMessage
from litestar.status_codes import HTTP_200_OK

from litestar_gateway.application.completion_service import CompletionService


async def _sse_native_events(
    events: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[ServerSentEventMessage]:
    """Anthropic Messages SSE wire format: named events (`event: <type>` +
    `data: <json>`), which the native `anthropic` SDK parses. Each raw event
    carries a `type` field — emit it as the SSE event name with the event JSON as
    the data. This is NOT the OpenAI `data:`-only + `[DONE]` format."""
    async for event in events:
        yield ServerSentEventMessage(data=json.dumps(event), event=event.get("type"))


@post(
    "/v1/messages",
    summary="Anthropic-native Messages (native passthrough)",
    description=(
        "Point the native `anthropic` SDK at the gateway. The request's `model` is "
        "the team model alias, resolved and guarded exactly like "
        "`/v1/chat/completions`. The native Anthropic response is returned verbatim "
        "(no OpenAI translation). Set `stream: true` to receive the raw Anthropic "
        "Messages Server-Sent Events (named events), relayed untranslated."
    ),
    status_code=HTTP_200_OK,
)
async def native_messages(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> Response[Any]:
    team_id = UUID(request.user)
    if data.get("stream"):
        # Native SSE relay: resolve + guard + prime eagerly so an open-time provider
        # error becomes an HTTP status before the 200 commits, then stream the raw
        # Anthropic events as named SSE events (Anthropic wire format, not OpenAI's).
        events = await completion_service.open_native_messages_stream(
            team_id, request.auth.id, data
        )
        return ServerSentEvent(_sse_native_events(events))
    # Non-streaming native dispatch: resolve + guard the model, meter the call,
    # and return the raw Anthropic Messages body untranslated.
    body = await completion_service.native_messages(team_id, request.auth.id, data)
    return Response(body, status_code=HTTP_200_OK)
