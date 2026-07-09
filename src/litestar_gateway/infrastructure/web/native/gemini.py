"""Gemini-native `generateContent` endpoints.

Authenticated by a team API key, so a customer can point the stock `google-genai`
client at our base URL:

    client = genai.Client(api_key="lsk_...", http_options=HttpOptions(base_url="https://.../"))
    client.models.generate_content(model="<team-model-alias>", contents=[...])

The `google-genai` SDK sends the key as `x-goog-api-key` (accepted by the shared
API-key middleware) and carries the model alias in the URL PATH — Gemini's wire
shape puts it there, not in the body. This handler is registered on the protected
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
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import ServerSentEvent
from litestar.response.sse import ServerSentEventMessage
from litestar.status_codes import HTTP_200_OK

from litestar_gateway.application.completion_service import CompletionService

_GENERATE = "generateContent"
_STREAM_GENERATE = "streamGenerateContent"


async def _sse_gemini_chunks(
    chunks: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[ServerSentEventMessage]:
    """Gemini streaming SSE wire format: `data: <json>` events with NO event name
    and NO `[DONE]` sentinel — exactly what the `google-genai` SDK parses when it
    requests `?alt=sse` (it reads lines prefixed with `data: `). Relay each raw
    `GenerateContentResponse` chunk as its own event. This is NOT the Anthropic
    named-event format, nor the OpenAI `data:`-only + `[DONE]` format."""
    async for chunk in chunks:
        yield ServerSentEventMessage(data=json.dumps(chunk))


@post(
    "/v1beta/models/{model_action:str}",
    summary="Gemini-native generateContent (native passthrough)",
    description=(
        "Point the native `google-genai` SDK at the gateway. The `{model_action}` "
        "path segment is `<team-model-alias>:generateContent` (non-streaming) or "
        "`<team-model-alias>:streamGenerateContent` (SSE). The alias is resolved and "
        "guarded like `/v1/chat/completions`; the native Gemini response is returned "
        "verbatim (no OpenAI translation). Streaming relays the raw Gemini "
        "`GenerateContentResponse` chunks as `data:` Server-Sent Events."
    ),
    status_code=HTTP_200_OK,
)
async def generate_content(
    request: Request,
    data: dict[str, Any],
    model_action: FromPath[str],
    completion_service: NamedDependency[CompletionService],
) -> Response[Any]:
    team_id = UUID(request.user)
    # Gemini's URL puts the model + method in one segment: `<alias>:<method>`.
    # Split on the last colon (aliases don't contain one) into (alias, method).
    alias, sep, method = model_action.rpartition(":")
    if not sep or not alias:
        raise NotFoundException("Unknown Gemini method")
    if method == _STREAM_GENERATE:
        # Native SSE relay: resolve + guard + prime eagerly so an open-time provider
        # error becomes an HTTP status before the 200 commits, then stream the raw
        # Gemini chunks as `data:` SSE events (Gemini wire format).
        chunks = await completion_service.open_generate_content_stream(
            team_id, request.auth.id, alias, data
        )
        return ServerSentEvent(_sse_gemini_chunks(chunks))
    if method == _GENERATE:
        # Non-streaming native dispatch: resolve + guard the model, meter the call,
        # and return the raw Gemini body untranslated.
        body = await completion_service.generate_content(team_id, request.auth.id, alias, data)
        return Response(body, status_code=HTTP_200_OK)
    raise NotFoundException(f"Unknown Gemini method '{method}'")
