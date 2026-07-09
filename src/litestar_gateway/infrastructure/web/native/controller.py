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

from typing import Any
from uuid import UUID

from litestar import Request, Response, post
from litestar.di import NamedDependency
from litestar.status_codes import HTTP_200_OK

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.domain.entities import ModelType
from litestar_gateway.domain.exceptions import UnsupportedOperation


@post(
    "/v1/messages",
    summary="Anthropic-native Messages (native passthrough)",
    description=(
        "Point the native `anthropic` SDK at the gateway. The request's `model` is "
        "the team model alias, resolved and guarded exactly like "
        "`/v1/chat/completions`. Provider dispatch is not implemented yet."
    ),
    status_code=HTTP_200_OK,
)
async def native_messages(
    request: Request,
    data: dict[str, Any],
    completion_service: NamedDependency[CompletionService],
) -> Response[Any]:
    # Reuse the OpenAI path's model-resolution + enable/type/credential guards
    # (no smart routing, no budget admission on the native surface yet). This
    # both proves the guards are wired and keeps the two surfaces in lockstep.
    await completion_service.prepare_native(UUID(request.user), ModelType.CHAT, data)
    # Guards passed and nothing was billed. Native provider dispatch arrives in a
    # later phase; until then, signal the capability gap the way the rest of the
    # gateway does (UnsupportedOperation -> 501).
    raise UnsupportedOperation("Native Anthropic Messages endpoint is not implemented yet")
