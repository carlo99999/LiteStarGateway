"""Billing and usage tracking entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities.enums import BudgetWindow


@dataclass(frozen=True)
class Budget:
    """A hard spend cap (USD) for a team over a recurring calendar window.

    Enforcement is pre-call: once the window's accumulated cost reaches
    `limit_cost`, further inference calls are rejected. Requests already in
    flight when the limit is crossed may still complete (bounded overshoot).

    `thresholds` (Plan 07 Phase 0, design doc §2/§8) are optional percentages
    of `limit_cost` — e.g. `[50, 80, 100]` — that drive proactive alerts as
    spend approaches the cap. Validate with `domain.budget.validate_thresholds`
    before constructing. Unused by any request path until Plan 07 Phase 1
    wires evaluation into settlement; enforcement above is unaffected."""

    id: UUID
    team_id: UUID
    limit_cost: float
    window: BudgetWindow  # noqa: F821
    created_at: datetime
    thresholds: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class BudgetAlertState:
    """Dedup ledger row: one per `(team_id, window, period_start, threshold)`
    that has already fired (Plan 07 Phase 0, design doc §2). `period_start`
    is the calendar anchor from `domain.budget.window_start` for `window` —
    the same anchor the pre-call budget gate uses. Persisted so the
    at-most-once-per-period guarantee survives process restarts; a new
    `period_start` (window rollover) is a new row, not a mutation of this
    one."""

    id: UUID
    team_id: UUID
    window: BudgetWindow  # noqa: F821
    period_start: datetime
    threshold: int
    fired_at: datetime


@dataclass(frozen=True)
class UsageEvent:
    """One recorded model call: token counts and estimated cost, tagged with the
    API key (when the caller used one) and model so usage can be broken down by
    either. Session-authenticated internal surfaces deliberately use ``None``."""

    id: UUID
    team_id: UUID
    api_key_id: UUID | None
    model_id: UUID
    model_name: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    created_at: datetime
    requested_alias: str | None = None
    resolved_model_id: UUID | None = None
    canonical_model_name: str | None = None
    callable_origin: str | None = None
    source_team_id: UUID | None = None
    # True when this event settled from the response cache (Plan 04 Phase 0)
    # rather than a real provider call — cost is always 0.0 on such an event.
    cache_hit: bool = False


@dataclass(frozen=True)
class UsageAttribution:
    """Requested callable identity captured before provider dispatch."""

    requested_alias: str | None
    callable_origin: str | None
    source_team_id: UUID | None


@dataclass(frozen=True)
class UsageAggregate:
    """Usage summed for one model (over an optional api-key/model filter)."""

    model_id: UUID
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    calls: int
    requested_alias: str | None = None
    resolved_model_id: UUID | None = None
    canonical_model_name: str | None = None
    callable_origin: str | None = None
    source_team_id: UUID | None = None


@dataclass(frozen=True)
class ApiKeySpend:
    """Accumulated usage/cost for one API key across all of its calls."""

    api_key_id: UUID
    prompt_tokens: int
    completion_tokens: int
    cost: float
    calls: int


@dataclass(frozen=True)
class TraceRecord:
    """One observability trace for a model call (metadata; no payload in v1)."""

    team_id: UUID
    api_key_id: UUID | None
    model_name: str
    provider: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    latency_ms: float
    status: str
    created_at: datetime
    # Exception class name when status == "error" (no message: keep traces secret-free).
    error_type: str | None = None
    # True when this trace observed a response-cache hit (Plan 04 Phase 0).
    cache_hit: bool = False
