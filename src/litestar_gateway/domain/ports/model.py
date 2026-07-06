"""Port — model deployment persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class ModelRepository(Protocol):
    """Persistence port for team-scoped model deployments."""

    async def add(self, model: Model) -> Model: ...

    async def get(self, model_id: UUID) -> Model | None: ...

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]: ...

    async def update(self, model: Model) -> Model: ...

    async def remove(self, model_id: UUID) -> None: ...
