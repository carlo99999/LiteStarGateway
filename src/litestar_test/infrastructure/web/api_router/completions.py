"""OpenAI-compatible inference endpoints.

Authenticated by a team API key (`Authorization: Bearer lsk_...`), so a customer
can point the stock OpenAI client at our base URL:

    client = OpenAI(api_key="lsk_...", base_url="https://.../")
    client.chat.completions.create(model="<team-model-alias>", messages=[...])

`request.user` is the team id (set by the API-key middleware); the request's
`model` is the team's model alias.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Request, post
from litestar.di import NamedDependency
from litestar.status_codes import HTTP_200_OK

from litestar_test.application.completion_service import CompletionService


@post(
    "/v1/chat/completions",
    summary="OpenAI-compatible chat completions",
    status_code=HTTP_200_OK,
)
async def chat_completions(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> dict[str, Any]:
    return await completion_service.chat_completion(UUID(request.user), data)


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
