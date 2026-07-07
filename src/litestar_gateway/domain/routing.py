"""Smart routing — domain contracts, candidate profiles, capability filters.

A *router* is a virtual model: a team-scoped alias backed by N candidate
models. Every request addressed to it is dispatched to one candidate, chosen
by a pluggable `RoutingStrategy`. The strategy only ever rewrites the model
name — the rest of the request pipeline (sanitizing, budget, metering) is
untouched.

Hard capability filters run BEFORE any strategy (deterministically): a
strategy chooses among capable candidates only. Zero survivors is a router
misconfiguration and fails the request with `NoRoutableCandidate`; exactly one
survivor skips the strategy entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class QualityTier(StrEnum):
    """Which complexity tier a candidate model serves."""

    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"
    REASONING = "REASONING"


_TIER_ORDER = (QualityTier.SIMPLE, QualityTier.MEDIUM, QualityTier.COMPLEX, QualityTier.REASONING)


@dataclass(frozen=True)
class CandidateModel:
    """A router candidate: a team model name plus its routing profile.

    The profile is declared in the router's config (not inferred): strategies
    consume `description`/`quality_tier`, the capability flags feed the hard
    filters, and the costs feed savings estimation."""

    model_name: str
    description: str
    quality_tier: QualityTier
    supports_vision: bool = False
    supports_tools: bool = False
    supports_json_schema: bool = False
    context_window_tokens: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None


@dataclass(frozen=True)
class RouterConfig:
    """A virtual model owned by a team. `default_model` is the §4 safety net:
    any strategy failure routes there instead of failing the request."""

    id: UUID
    team_id: UUID
    name: str
    candidates: tuple[CandidateModel, ...]
    default_model: str
    strategy: str
    strategy_config: dict[str, Any]
    enabled: bool
    created_at: datetime
    shadow_strategy: str | None = None


@dataclass(frozen=True)
class RoutingContext:
    """What a strategy may look at, extracted once from the request."""

    user_text: str
    system_prompt: str | None
    estimated_input_tokens: int
    has_images: bool
    has_tools: bool
    wants_json_schema: bool
    requested_max_tokens: int | None
    team_id: UUID | None = None
    api_key_id: UUID | None = None
    # The router's safety net, visible to strategies that need a "no match"
    # outcome distinct from failure (e.g. semantic routes below threshold).
    default_model: str | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """A strategy's verdict. `signals` are human-readable trigger notes."""

    model_name: str
    strategy: str
    tier: str | None
    score: float | None
    signals: tuple[str, ...]
    decision_ms: float


@dataclass(frozen=True)
class RoutingDecisionRecord:
    """One persisted routing decision (observability, §7)."""

    id: UUID
    team_id: UUID
    router_name: str
    strategy: str
    chosen_model: str
    tier: str | None
    score: float | None
    signals: tuple[str, ...]
    decision_ms: float
    is_shadow: bool
    fallback_used: bool
    api_key_id: UUID | None
    created_at: datetime
    # Unit costs captured at decision time: the chosen candidate's and the most
    # expensive capable candidate's ("what this request would have cost").
    chosen_input_cost: float | None = None
    chosen_output_cost: float | None = None
    alt_input_cost: float | None = None
    alt_output_cost: float | None = None
    # Actual usage, filled in after settlement (None for streams in phase 3).
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RoutingStrategy(Protocol):
    """Port: pick one candidate for the request. Implementations must never
    mutate their inputs; failures are handled by the caller (fallback to
    `default_model`), so raising is acceptable but never fatal to the user."""

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision: ...


def estimate_tokens(text: str) -> int:
    """~4 characters per token — the same cheap heuristic the scorer uses."""
    return len(text) // 4


def _flatten_content(content: Any) -> str:
    """A message's text: strings pass through, multimodal block lists are
    flattened to their text parts (Chat Completions and Responses shapes)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "input_text")
        ]
        return " ".join(part for part in parts if part).strip()
    return ""


def _content_has_image(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in ("image_url", "input_image")
        for block in content
    )


def build_routing_context(
    request: dict[str, Any],
    *,
    team_id: UUID | None = None,
    api_key_id: UUID | None = None,
) -> RoutingContext:
    """Extract the routing context from an OpenAI-shaped request — written once
    and shared by every strategy. Handles Chat Completions `messages` and the
    Responses API `input` (string or item list)."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        raw_input = request.get("input")
        if isinstance(raw_input, str):
            messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            messages = [item for item in raw_input if isinstance(item, dict)]
        else:
            messages = []

    user_text = ""
    system_prompt: str | None = None
    has_images = False
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content")
        has_images = has_images or _content_has_image(content)
        text = _flatten_content(content)
        if role == "user" and not user_text and text:
            user_text = text
        elif role in ("system", "developer") and system_prompt is None and text:
            system_prompt = text

    response_format = request.get("response_format")
    wants_json_schema = (
        isinstance(response_format, dict) and response_format.get("type") == "json_schema"
    )
    text_format = request.get("text")
    if isinstance(text_format, dict):
        fmt = text_format.get("format")
        wants_json_schema = wants_json_schema or (
            isinstance(fmt, dict) and fmt.get("type") == "json_schema"
        )

    max_tokens = next(
        (
            request[key]
            for key in ("max_completion_tokens", "max_output_tokens", "max_tokens")
            if isinstance(request.get(key), int)
        ),
        None,
    )
    return RoutingContext(
        user_text=user_text,
        system_prompt=system_prompt,
        estimated_input_tokens=estimate_tokens(f"{system_prompt or ''} {user_text}"),
        has_images=has_images,
        has_tools=bool(request.get("tools")),
        wants_json_schema=wants_json_schema,
        requested_max_tokens=max_tokens,
        team_id=team_id,
        api_key_id=api_key_id,
    )


def filter_candidates(
    ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
) -> tuple[CandidateModel, ...]:
    """The hard capability filters (§3) — run before any strategy."""

    def capable(candidate: CandidateModel) -> bool:
        if ctx.has_images and not candidate.supports_vision:
            return False
        if ctx.has_tools and not candidate.supports_tools:
            return False
        if ctx.wants_json_schema and not candidate.supports_json_schema:
            return False
        if (
            candidate.context_window_tokens is not None
            and ctx.estimated_input_tokens > candidate.context_window_tokens
        ):
            return False
        return True

    return tuple(candidate for candidate in candidates if capable(candidate))


def nearest_tier_candidate(
    tier: QualityTier, candidates: tuple[CandidateModel, ...]
) -> CandidateModel | None:
    """The candidate serving `tier`, or the nearest tier below it, or above.
    Deterministic: within a tier the first declared candidate wins."""
    by_tier: dict[QualityTier, CandidateModel] = {}
    for candidate in candidates:
        by_tier.setdefault(candidate.quality_tier, candidate)
    index = _TIER_ORDER.index(tier)
    for candidate_tier in (*reversed(_TIER_ORDER[: index + 1]), *_TIER_ORDER[index + 1 :]):
        if candidate_tier in by_tier:
            return by_tier[candidate_tier]
    return None
