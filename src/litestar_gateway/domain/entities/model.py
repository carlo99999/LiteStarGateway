"""Model and credential entities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from litestar_gateway.domain.exceptions import InvalidModelCapabilities

from .enums import ModelType, Provider

# Gateway operations a model may be declared to serve (Plan 18). Mirrors the
# operation strings in infrastructure/llm/gateway.py.
CHAT_CAPABILITY = "chat.completions"
DECLARABLE_CAPABILITIES = frozenset({CHAT_CAPABILITY, "embeddings", "image_generation"})
# Fail-closed: an `openai_compatible` model that declares nothing serves chat
# and nothing else, so an under-declared model serves less, never more.
DEFAULT_CAPABILITIES = frozenset({CHAT_CAPABILITY})


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
    cache_write_cost_per_token: float | None = None
    cache_read_cost_per_token: float | None = None
    # Image generation: a flat per-image fallback plus optional size/quality-
    # specific overrides keyed by `pricing.image_price_key(size, quality)`.
    image_cost_per_image: float | None = None
    image_prices: dict[str, float] = field(default_factory=dict)
    # Which gateway operations this model serves (Plan 18). Only consulted for
    # `openai_compatible`, where the provider alone cannot say what a given
    # backend does — every other provider's operation set is a property of the
    # provider and stays declared in the gateway registry. Declared, never
    # probed: the gateway does not call upstream to discover capabilities.
    capabilities: frozenset[str] = DEFAULT_CAPABILITIES

    def merge_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """Effective request for a provider call: admin `params` (defaults the
        client may override), then the sanitized client `request`, then
        `params_enforced` (admin policy the client cannot override)."""
        return {**self.params, **request, **self.params_enforced}


def normalize_capabilities(provider: Provider, declared: Iterable[str] | None) -> frozenset[str]:
    """Validate an operator's capability declaration and fold it to the set the
    entity carries.

    Omitted or empty ⇒ the chat-only default, so under-declaring serves less
    rather than more. Raises `InvalidModelCapabilities` (→ 400) for an unknown
    operation, or for any
    declaration on a provider whose operation set is a fixed property of the
    provider — silently ignoring one there would leave an operator believing
    they had constrained a model they had not.
    """
    if declared is None:
        return DEFAULT_CAPABILITIES
    capabilities = frozenset(declared)
    if not capabilities:
        return DEFAULT_CAPABILITIES
    unknown = sorted(capabilities - DECLARABLE_CAPABILITIES)
    if unknown:
        raise InvalidModelCapabilities(
            f"unknown capabilities: {', '.join(unknown)}; "
            f"declarable: {', '.join(sorted(DECLARABLE_CAPABILITIES))}"
        )
    if provider is not Provider.OPENAI_COMPATIBLE and capabilities != DEFAULT_CAPABILITIES:
        raise InvalidModelCapabilities(
            f"capabilities may only be declared for '{Provider.OPENAI_COMPATIBLE}' models; "
            f"provider '{provider}' advertises a fixed operation set"
        )
    return capabilities


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
