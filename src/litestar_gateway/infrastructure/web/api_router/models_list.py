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

from litestar_gateway.application.model_service import ModelService
from litestar_gateway.application.routing.service import RouterService

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

    # Every callable model, by its effective alias (own + extended + global).
    callable_models = await model_service.list_callable(team_id)
    routers = await router_service.list_by_team(team_id)

    data = [
        _entry(c.alias, int(c.model.created_at.timestamp()))
        for c in callable_models
        if c.model.enabled
    ] + [_entry(r.name, int(r.created_at.timestamp())) for r in routers if r.enabled]
    data.sort(key=lambda entry: entry["id"])
    return {"object": "list", "data": data}
