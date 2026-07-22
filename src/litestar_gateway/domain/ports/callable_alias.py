"""Port for the unified callable-alias registry."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.callable_alias import CallableAliasBinding
from litestar_gateway.domain.entities import Model


class CallableAliasRepository(Protocol):
    async def snapshot(
        self, team_id: UUID | None
    ) -> tuple[list[CallableAliasBinding], set[str]]: ...

    async def list_explicit(self, team_id: UUID | None) -> list[CallableAliasBinding]: ...

    async def list_reserved(self, team_id: UUID | None) -> set[str]: ...

    async def slot_reserved(self, team_id: UUID | None, alias: str) -> bool: ...


class CallableModelResolver(Protocol):
    """Resolve concrete model aliases within one repository unit of work."""

    async def resolve_model(self, team_id: UUID | None, alias: str) -> Model | None: ...
