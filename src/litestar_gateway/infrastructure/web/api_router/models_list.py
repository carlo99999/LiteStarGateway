"""OpenAI-compatible `GET /v1/models`.

Lists the models a team can call by their alias — its own enabled models, the
models extended to it, the enabled global models, plus its enabled routers
(virtual models), since all are valid values for the `model` field on the
inference endpoints. Authenticated by the team API key (the router's auth
middleware); `request.user` is the team id.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Request, get
from litestar.di import NamedDependency

from litestar_gateway.application.callable_aliases import CallableAliasResolver

_OWNER = "litestar-gateway"


def _entry(
    alias: str, created: int, resource_type: str, resource_id: UUID, binding_id: UUID
) -> dict[str, Any]:
    return {
        "id": alias,
        "object": "model",
        "created": created,
        "owned_by": _OWNER,
        "type": resource_type,
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "binding_id": str(binding_id),
    }


@get("/v1/models", summary="OpenAI-compatible list of the team's callable models")
async def list_models(
    request: Request,
    callable_resolver: NamedDependency[CallableAliasResolver],
) -> dict[str, Any]:
    team_id = UUID(request.user)

    resolved = await callable_resolver.list_callable(team_id)
    data = [
        _entry(
            item.effective_alias,
            int(item.resource.created_at.timestamp()),
            item.kind.value,
            item.resource_id,
            item.binding.id,
        )
        for item in resolved
        if item.resource.enabled
    ]
    return {"object": "list", "data": data}
