"""Ports — router (virtual model) persistence + routing-decision log."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.routing import RouterConfig, RoutingDecisionRecord


class RouterRepository(Protocol):
    """Persistence port for routers."""

    async def add(self, router: RouterConfig) -> RouterConfig: ...

    async def get(self, team_id: UUID, router_id: UUID) -> RouterConfig | None: ...

    async def get_by_name(self, team_id: UUID, name: str) -> RouterConfig | None: ...

    async def list_by_team(self, team_id: UUID) -> list[RouterConfig]: ...

    async def update(self, router: RouterConfig) -> RouterConfig: ...

    async def delete(self, team_id: UUID, router_id: UUID) -> bool: ...


class RoutingDecisionLog(Protocol):
    """Append-only log of routing decisions (observability, §7)."""

    async def record(self, decision: RoutingDecisionRecord) -> None: ...
