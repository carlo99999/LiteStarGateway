"""One deterministic resolver for every callable model and router alias."""

from __future__ import annotations

import dataclasses
from uuid import UUID

from litestar_gateway.domain.callable_alias import CallableAliasBinding, CallableKind
from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.ports import (
    CallableAliasRepository,
    ModelRepository,
    RouterRepository,
)
from litestar_gateway.domain.routing import RouterConfig

type CallableResource = Model | RouterConfig


@dataclasses.dataclass(frozen=True)
class ResolvedCallable:
    """An effective alias bound to a stable, explicitly typed identity."""

    effective_alias: str
    binding: CallableAliasBinding
    resource: CallableResource

    @property
    def kind(self) -> CallableKind:
        return self.binding.kind

    @property
    def resource_id(self) -> UUID:
        return self.binding.resource_id


class CallableAliasResolver:
    def __init__(
        self,
        aliases: CallableAliasRepository,
        models: ModelRepository,
        routers: RouterRepository,
    ) -> None:
        self._aliases = aliases
        self._models = models
        self._routers = routers

    async def _load(self, binding: CallableAliasBinding) -> CallableResource | None:
        if binding.kind is CallableKind.MODEL:
            return await self._models.get(binding.resource_id)
        if binding.router_grant_id is not None:
            return await self._routers.get_for_grant(binding.router_grant_id, binding.team_id)
        if binding.router_revision_id is not None:
            return await self._routers.get_revision(binding.resource_id, binding.router_revision_id)
        return await self._routers.get_any(binding.resource_id)

    @staticmethod
    def _effective_bindings(
        explicit: list[CallableAliasBinding], reserved: set[str], team_id: UUID | None
    ) -> dict[str, CallableAliasBinding]:
        """Allocate effective aliases without loading target resources."""
        local = [
            binding for binding in explicit if team_id is not None and binding.team_id == team_id
        ]
        global_bindings = [binding for binding in explicit if binding.team_id is None]
        by_alias: dict[str, CallableAliasBinding] = {
            binding.alias: binding for binding in sorted(local, key=lambda b: (b.alias, b.id))
        }
        occupied = set(by_alias) | reserved
        # Synthetic aliases must not steal another global's declared canonical
        # name (e.g. shadowed ``foo`` must skip declared ``foo-global``).
        declared_globals = {binding.alias for binding in global_bindings}
        for binding in sorted(global_bindings, key=lambda b: (b.alias, b.id)):
            effective = binding.alias
            if effective in occupied:
                effective = f"{binding.alias}-global"
                suffix = 2
                while effective in occupied or effective in declared_globals:
                    effective = f"{binding.alias}-global-{suffix}"
                    suffix += 1
            by_alias[effective] = binding
            occupied.add(effective)
        return by_alias

    async def list_callable(self, team_id: UUID | None) -> list[ResolvedCallable]:
        """Build the complete namespace from one registry snapshot.

        Local bindings own their declared slot. Globals retain their declared
        alias when free; otherwise they receive the first free deterministic
        ``-global``/``-global-N`` alias. Nothing is silently dropped.
        """
        explicit, reserved = await self._aliases.snapshot(team_id)
        by_alias = self._effective_bindings(explicit, reserved, team_id)

        resolved: list[ResolvedCallable] = []
        for effective, binding in sorted(by_alias.items()):
            resource = await self._load(binding)
            if resource is not None:
                resolved.append(ResolvedCallable(effective, binding, resource))
        return resolved

    async def resolve(self, team_id: UUID | None, alias: str) -> ResolvedCallable | None:
        explicit, reserved = await self._aliases.snapshot(team_id)
        binding = self._effective_bindings(explicit, reserved, team_id).get(alias)
        if binding is None:
            return None
        resource = await self._load(binding)
        return ResolvedCallable(alias, binding, resource) if resource is not None else None

    async def resolve_model(self, team_id: UUID | None, alias: str) -> Model | None:
        resolved = await self.resolve(team_id, alias)
        if resolved is None or resolved.kind is not CallableKind.MODEL:
            return None
        assert isinstance(resolved.resource, Model)
        return resolved.resource

    async def resolve_model_id(self, team_id: UUID | None, model_id: UUID) -> Model | None:
        """Load an exact model identity only when it is callable in this scope.

        Router revisions use this path so a later alias rename or a target-team
        homonym can never redirect an already-approved candidate.
        """
        explicit, _ = await self._aliases.snapshot(team_id)
        if not any(
            binding.kind is CallableKind.MODEL and binding.resource_id == model_id
            for binding in explicit
        ):
            return None
        return await self._models.get(model_id)

    async def explicit_taken(self, team_id: UUID | None, alias: str) -> bool:
        rows = await self._aliases.list_explicit(team_id)
        return any(row.team_id == team_id and row.alias == alias for row in rows)

    async def slot_reserved(self, team_id: UUID | None, alias: str) -> bool:
        return await self._aliases.slot_reserved(team_id, alias)
