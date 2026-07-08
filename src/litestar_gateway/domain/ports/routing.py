"""Ports — router (virtual model) persistence + routing-decision log."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.ports.credential import CredentialRepository
from litestar_gateway.domain.ports.model import ModelRepository
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
    """Log of routing decisions (observability, §7): append + read/aggregate."""

    async def record(self, decision: RoutingDecisionRecord) -> None: ...

    async def update_usage(
        self, decision_id: UUID, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Attach the request's actual token usage after settlement."""
        ...

    async def list_decisions(
        self,
        team_id: UUID,
        router_name: str,
        *,
        strategy: str | None = None,
        chosen_model: str | None = None,
        is_shadow: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RoutingDecisionRecord]: ...

    async def distribution(
        self, team_id: UUID, router_name: str
    ) -> list[tuple[str, str | None, bool, int]]:
        """(chosen_model, tier, is_shadow, count) rows for the router."""
        ...

    async def savings(self, team_id: UUID, router_name: str) -> tuple[float, int, int]:
        """(total_estimated_savings, decisions_counted, decisions_without_usage)
        over non-shadow decisions: Σ (alt−chosen unit cost) × actual tokens."""
        ...


@runtime_checkable
class RoutingDecisionLogFactory(Protocol):
    """Opens a decision log with its OWN unit of work — for shadow-mode tasks
    that outlive the request (a request-scoped session would already be
    closed by the time a fire-and-forget shadow run persists its verdict)."""

    def __call__(self) -> AbstractAsyncContextManager[RoutingDecisionLog]: ...


@runtime_checkable
class RoutingRepositoryFactory(Protocol):
    """Opens model/credential repositories with their OWN unit of work — for
    shadow-mode strategy lookups (judge/embeddings): the fire-and-forget
    shadow task races the request coroutine, whose scoped session is not
    safe for concurrent cross-task use."""

    def __call__(
        self,
    ) -> AbstractAsyncContextManager[tuple[ModelRepository, CredentialRepository]]: ...
