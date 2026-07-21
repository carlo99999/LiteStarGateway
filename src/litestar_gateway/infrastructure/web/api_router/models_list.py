"""OpenAI-compatible `GET /v1/models`.

Lists the models a team can call by their alias — the team's enabled models
plus its enabled routers (virtual models), since both are valid values for the
`model` field on the inference endpoints. Authenticated by the team API key
(the router's auth middleware); `request.user` is the team id.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Request, get
from litestar.di import NamedDependency

from litestar_gateway.application.model_service import ModelService
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE

_OWNER = "litestar-gateway"


def _entry(alias: str, created: int) -> dict[str, Any]:
    return {"id": alias, "object": "model", "created": created, "owned_by": _OWNER}


@get("/v1/models", summary="OpenAI-compatible list of the team's callable models")
async def list_models(
    request: Request,
    model_service: NamedDependency[ModelService],
    router_service: NamedDependency[RouterService],
) -> dict[str, Any]:
    team_id = UUID(request.user)

    # Page through all models (the catalog must be complete, not first-page-only).
    models = []
    offset = 0
    while True:
        page = await model_service.list_for_team(team_id, limit=DEFAULT_PAGE_SIZE, offset=offset)
        models.extend(page)
        if len(page) < DEFAULT_PAGE_SIZE:
            break
        offset += len(page)

    routers = await router_service.list_by_team(team_id)

    data = [_entry(m.name, int(m.created_at.timestamp())) for m in models if m.enabled] + [
        _entry(r.name, int(r.created_at.timestamp())) for r in routers if r.enabled
    ]
    data.sort(key=lambda entry: entry["id"])
    return {"object": "list", "data": data}
