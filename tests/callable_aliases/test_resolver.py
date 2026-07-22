"""Pure allocation and bounded-load tests for the callable resolver."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.domain.callable_alias import (
    CallableAliasBinding,
    CallableKind,
    CallableOrigin,
)


def _binding(alias: str, kind: CallableKind, team_id: UUID | None) -> CallableAliasBinding:
    return CallableAliasBinding(
        id=uuid4(),
        team_id=team_id,
        alias=alias,
        kind=kind,
        resource_id=uuid4(),
        origin=CallableOrigin.GLOBAL if team_id is None else CallableOrigin.OWN,
        source_team_id=team_id,
    )


class Aliases:
    def __init__(self, bindings: list[CallableAliasBinding]) -> None:
        self.bindings = bindings
        self.snapshots = 0

    async def snapshot(self, team_id):
        self.snapshots += 1
        return self.bindings, set()


class Resources:
    def __init__(self) -> None:
        self.loads: list[UUID] = []

    async def get(self, resource_id: UUID):
        self.loads.append(resource_id)
        return SimpleNamespace(id=resource_id)

    async def get_any(self, resource_id: UUID):
        self.loads.append(resource_id)
        return SimpleNamespace(id=resource_id)


async def test_resolve_loads_only_the_selected_resource() -> None:
    team_id = uuid4()
    bindings = [_binding(f"model-{index}", CallableKind.MODEL, team_id) for index in range(20)] + [
        _binding(f"router-{index}", CallableKind.ROUTER, team_id) for index in range(20)
    ]
    aliases = Aliases(bindings)
    models = Resources()
    routers = Resources()
    resolver = CallableAliasResolver(aliases, models, routers)  # type: ignore[arg-type]

    selected = await resolver.resolve(team_id, "model-7")

    assert selected is not None
    assert selected.resource_id == bindings[7].resource_id
    assert aliases.snapshots == 1
    assert models.loads == [bindings[7].resource_id]
    assert routers.loads == []


async def test_resolve_model_id_requires_an_accessible_model_binding() -> None:
    team_id = uuid4()
    accessible = _binding("approved", CallableKind.MODEL, team_id)
    inaccessible_id = uuid4()
    aliases = Aliases([accessible])
    models = Resources()
    resolver = CallableAliasResolver(aliases, models, Resources())  # type: ignore[arg-type]

    allowed = await resolver.resolve_model_id(team_id, accessible.resource_id)
    denied = await resolver.resolve_model_id(team_id, inaccessible_id)

    assert allowed is not None
    assert allowed.id == accessible.resource_id
    assert denied is None
    assert models.loads == [accessible.resource_id]
