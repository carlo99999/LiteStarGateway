"""Port — model deployment persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Model, ModelGrant
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class ModelRepository(Protocol):
    """Persistence port for model deployments (team-owned or global) and the
    grants that extend a team model to other teams."""

    async def add(self, model: Model) -> Model: ...

    async def get(self, model_id: UUID) -> Model | None: ...

    async def get_by_name(self, team_id: UUID | None, name: str) -> Model | None:
        """Resolve the model a team calls by `name`, in priority order:
        the team's own model → a model extended to it under that alias → a
        global model of that name → the `<base>-global` form when the team's
        own `<base>` shadows a global. `team_id=None` resolves against global
        models only (used to validate a global router's candidates). None when
        nothing matches."""
        ...

    async def name_taken_in_team(self, team_id: UUID, name: str) -> bool:
        """True if the team already uses `name` for one of its OWN models or as
        an extended alias. Used to reject a colliding create; unlike
        `get_by_name` it ignores global models (a team may shadow a global)."""
        ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]:
        """The team's OWN models (not globals or extended ones)."""
        ...

    async def list_global(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]: ...

    async def all_global(self) -> list[Model]:
        """Every global model (unpaged); for building a team's callable set."""
        ...

    async def update(self, model: Model) -> Model: ...

    async def remove(self, model_id: UUID) -> None: ...

    async def exists_for_credential(self, credential_id: UUID) -> bool:
        """True if any model (in any team) references this credential."""
        ...

    # Grants (extending a team model to other teams).

    async def add_grant(self, grant: ModelGrant) -> ModelGrant: ...

    async def get_grant(self, grant_id: UUID) -> ModelGrant | None: ...

    async def remove_grant(self, grant_id: UUID) -> None: ...

    async def list_grants_for_model(self, model_id: UUID) -> list[ModelGrant]:
        """The teams a model is extended to."""
        ...

    async def list_grants_for_team(self, team_id: UUID) -> list[ModelGrant]:
        """The models extended into a team."""
        ...
