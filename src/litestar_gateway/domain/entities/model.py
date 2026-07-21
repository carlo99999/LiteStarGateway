"""Model and credential entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from .enums import ModelType, Provider


@dataclass(frozen=True)
class Credential:
    """Metadata for a provider credential. Secret values are stored encrypted
    by the repository and never live on this entity."""

    id: UUID
    name: str
    provider: Provider
    created_at: datetime


@dataclass(frozen=True)
class Model:
    """A configured model deployment.

    Owned by a team (`team_id` set) or by the platform (`team_id is None`, a
    "global" model callable by every team, present and future). `provider` must
    match the referenced credential's provider (enforced on write).
    """

    id: UUID
    team_id: UUID | None
    name: str
    provider: Provider
    credential_id: UUID
    type: ModelType
    provider_model_id: str  # upstream model name, e.g. "gpt-4o"
    params: dict[str, Any]  # client-overridable default LLM params (temperature, ...)
    # Note: no api_base here — the endpoint comes from the (admin-managed)
    # credential, so a team admin cannot redirect the credential's secret.
    api_version: str | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    enabled: bool
    created_at: datetime
    # Admin policy the client cannot override (applied last in the merge), e.g. a
    # forced response_format or a locked tool_choice. Distinct from `params`,
    # which are defaults the client may override.
    params_enforced: dict[str, Any] = field(default_factory=dict)
    # Per-model output-token ceiling. When set, client output-token fields are
    # clamped down to it (min semantics) and it is injected when the client omits
    # one, so it is a real cap — not bypassable by omission. None = no cap.
    max_output_tokens: int | None = None

    def merge_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """Effective request for a provider call: admin `params` (defaults the
        client may override), then the sanitized client `request`, then
        `params_enforced` (admin policy the client cannot override)."""
        return {**self.params, **request, **self.params_enforced}


@dataclass(frozen=True)
class ModelGrant:
    """An "extension" of a team-owned model to another team.

    The grant points at the source `Model` (single source of truth — costs and
    config are read from it, never copied), and carries the `alias` the target
    team calls it by. The alias defaults to the source model's name and is
    suffixed to avoid a clash with a name the target team already uses.
    """

    id: UUID
    model_id: UUID
    team_id: UUID  # the team the model is extended to
    alias: str
    created_at: datetime
