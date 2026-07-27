"""Model and credential entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
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
    # Money rates are exact ``Decimal`` (Plan 13 Phase 2 — domain.money); token
    # counts stay ``int``. ``None`` ⇒ the dimension bills at zero.
    input_cost_per_token: Decimal | None
    output_cost_per_token: Decimal | None
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
    # The team that originally owned this model, preserved even after it is
    # promoted to global — so a global model can still show its provenance
    # ("global · from Team X"). None for a model created global from the start.
    origin_team_id: UUID | None = None
    # Response cache opt-in (Plan 04 Phase 0), per team+model: exact-match
    # caching is off by default even when the global RESPONSE_CACHE_ENABLED
    # kill-switch is on. `cache_allow_nondeterministic` additionally opts into
    # caching requests with `temperature > 0` (refused by default — design §7).
    cache_enabled: bool = False
    cache_allow_nondeterministic: bool = False
    # Semantic-tier opt-in (Plan 04 Phase 2), separate from `cache_enabled`:
    # the semantic tier is never built without exact-match in front of it, so
    # this only ever takes effect when `cache_enabled` is also true, but
    # exact-match may be on while this stays off (design §1/§7).
    cache_semantic_enabled: bool = False
    # Non-token pricing (Plan 13 Phase 1, design §1). All optional and strictly
    # opt-in: unset ⇒ the dimension bills at zero, exactly as before this plan.
    # Anthropic prompt-cache rates are kept distinct from `input_cost_per_token`
    # because cache-write/read economics and audit meaning differ from ordinary
    # input tokens.
    cache_write_cost_per_token: Decimal | None = None
    cache_read_cost_per_token: Decimal | None = None
    # Image generation: a flat per-image fallback plus optional size/quality-
    # specific overrides keyed by `pricing.image_price_key(size, quality)`.
    image_cost_per_image: Decimal | None = None
    image_prices: dict[str, Decimal] = field(default_factory=dict)

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
