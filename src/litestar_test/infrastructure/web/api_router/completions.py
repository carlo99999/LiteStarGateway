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
from litestar.status_codes import HTTP_200_OK

from litestar_test.application.completion_service import CompletionService


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
        chunks = await completion_service.open_chat_stream(team_id, data)
        return ServerSentEvent(_sse_events(chunks))
    return Response(await completion_service.chat_completion(team_id, data))


@post(
    "/v1/responses",
    summary="OpenAI-compatible Responses API",
    description=(
        "OpenAI and Azure use the provider's native Responses API (full feature "
        "set). Providers without one (e.g. Databricks) are **emulated** over "
        "chat.completions: text-in/text-out works, but tools, structured outputs, "
        "multimodal input and stateful conversations are not supported."
    ),
    status_code=HTTP_200_OK,
)
async def responses(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> dict[str, Any]:
    return await completion_service.responses(UUID(request.user), data)


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
    return await completion_service.embeddings(UUID(request.user), data)
