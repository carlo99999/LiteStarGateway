"""Ports — router (virtual model) persistence + routing-decision log."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.ports.credential import CredentialRepository
from litestar_gateway.domain.ports.model import ModelRepository
from litestar_gateway.domain.routing import RouterConfig, RouterGrant, RoutingDecisionRecord


class RouterRepository(Protocol):
    """Persistence port for routers (team-owned or global) and extension grants."""

    async def add(self, router: RouterConfig) -> RouterConfig: ...

    async def get(self, team_id: UUID, router_id: UUID) -> RouterConfig | None: ...

    async def get_any(self, router_id: UUID) -> RouterConfig | None: ...

    async def get_by_name(self, team_id: UUID, name: str) -> RouterConfig | None:
        """Resolve the router a team calls by `name`: own → extended → global →
        `<base>-global` when the team's own `<base>` shadows a global."""
        ...

    async def name_taken_in_team(self, team_id: UUID, name: str) -> bool: ...

    async def list_by_team(self, team_id: UUID) -> list[RouterConfig]: ...

    async def list_global(self) -> list[RouterConfig]: ...

    async def all_global(self) -> list[RouterConfig]: ...

    async def update(self, router: RouterConfig) -> RouterConfig: ...

    async def delete(self, team_id: UUID, router_id: UUID) -> bool: ...

    async def delete_global(self, router_id: UUID) -> bool: ...

    async def promote_to_global(self, router_id: UUID) -> RouterConfig | None: ...

    async def add_grant(self, grant: RouterGrant) -> RouterGrant: ...

    async def get_grant(self, grant_id: UUID) -> RouterGrant | None: ...

    async def remove_grant(self, grant_id: UUID) -> None: ...

    async def list_grants_for_router(self, router_id: UUID) -> list[RouterGrant]: ...

    async def list_grants_for_team(self, team_id: UUID) -> list[RouterGrant]: ...


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
        router_id: UUID,
        *,
        strategy: str | None = None,
        chosen_model: str | None = None,
        is_shadow: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RoutingDecisionRecord]: ...

    async def distribution(
        self, team_id: UUID, router_id: UUID
    ) -> list[tuple[str, str | None, bool, int]]:
        """(chosen_model, tier, is_shadow, count) rows for the router."""
        ...

    async def savings(self, team_id: UUID, router_id: UUID) -> tuple[float, int, int]:
        """(total_estimated_savings, decisions_counted, decisions_without_usage)
        over one router's non-shadow decisions: Σ (alt−chosen unit cost) ×
        actual tokens. Keyed by router id, so a deleted router's history never
        leaks into a later router that reused its name."""
        ...

    async def platform_savings(self) -> tuple[float, int, int]:
        """The same savings aggregate across every team and router — the
        platform-wide "what smart routing saved" figure (admin dashboard)."""
        ...

    async def team_savings(self, team_id: UUID) -> tuple[float, int, int]:
        """The savings aggregate for one team across all of its routers."""
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
