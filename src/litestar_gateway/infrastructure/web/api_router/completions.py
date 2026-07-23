"""OpenAI-compatible inference endpoints.

Authenticated by a team API key (`Authorization: Bearer lsk_...`), so a customer
can point the stock OpenAI client at our base URL:

    client = OpenAI(api_key="lsk_...", base_url="https://.../")
    client.chat.completions.create(model="<team-model-alias>", messages=[...])

`request.user` is the team id (set by the API-key middleware); the request's
`model` is the team's model alias.
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


async def _sse_response_events(
    events: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[ServerSentEventMessage]:
    """Responses-API wire format: typed SSE events (`event:` + `data:`), no [DONE]."""
    async for event in events:
        yield ServerSentEventMessage(data=json.dumps(event), event=event.get("type"))


async def _sse_events(chunks: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    """OpenAI streaming wire format: each chunk as a `data:` event, then `[DONE]`."""
    async for chunk in chunks:
        yield json.dumps(chunk)
    yield "[DONE]"


@post(
    "/v1/chat/completions",
    summary="OpenAI-compatible chat completions",
    description="Set `stream: true` to receive Server-Sent Events (OpenAI chunk format).",
    status_code=HTTP_200_OK,
)
async def chat_completions(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> Response[Any]:
    # Return Response (not a union) so the SSE branch keeps its text/event-stream
    # content type — a union return makes Litestar force application/json.
    team_id = UUID(request.user)
    if data.get("stream"):
        # Resolve eagerly so errors become a proper HTTP status before the SSE starts.
        chunks = await completion_service.open_chat_stream(team_id, request.auth.id, data)
        return ServerSentEvent(_sse_events(chunks))
    return Response(await completion_service.chat_completion(team_id, request.auth.id, data))


@post(
    "/v1/responses",
    summary="OpenAI-compatible Responses API",
    description=(
        "OpenAI and Azure use the provider's native Responses API for the "
        "governed synchronous, stateless SDK surface. Providers without one are "
        "**emulated** over chat.completions: text and structured outputs work; "
        "Databricks and Anthropic also support non-streaming function-tool "
        "loops. Streaming tools, other providers' emulated tools, multimodal input, stateful "
        "conversations, background execution and client-selected service tiers "
        "fail explicitly with 501."
    ),
    status_code=HTTP_200_OK,
)
async def responses(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> Response[Any]:
    team_id = UUID(request.user)
    if data.get("stream") is True:
        events = await completion_service.open_responses_stream(team_id, request.auth.id, data)
        return ServerSentEvent(_sse_response_events(events))
    return Response(await completion_service.responses(team_id, request.auth.id, data))


@post(
    "/v1/embeddings",
    summary="OpenAI-compatible embeddings",
    description="Requires a model of type `embeddings`. Supported on OpenAI, Azure and Databricks.",
    status_code=HTTP_200_OK,
)
async def embeddings(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> dict[str, Any]:
    return await completion_service.embeddings(UUID(request.user), request.auth.id, data)


@post(
    "/v1/images/generations",
    summary="OpenAI-compatible image generation",
    description="Requires a model of type `image`. Supported on OpenAI and Azure.",
    status_code=HTTP_200_OK,
)
async def images(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> dict[str, Any]:
    return await completion_service.images(UUID(request.user), request.auth.id, data)
