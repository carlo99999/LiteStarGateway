"""Billing and usage tracking entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities.enums import BudgetWindow


@dataclass(frozen=True)
class Budget:
    """A hard spend cap (USD) for a team over a recurring calendar window.

    Enforcement is pre-call: once the window's accumulated cost reaches
    `limit_cost`, further inference calls are rejected. Requests already in
    flight when the limit is crossed may still complete (bounded overshoot)."""

    id: UUID
    team_id: UUID
    limit_cost: float
    window: BudgetWindow  # noqa: F821
    created_at: datetime


@dataclass(frozen=True)
class UsageEvent:
    """One recorded model call: token counts and estimated cost, tagged with the
    API key and model so usage can be broken down by either."""

    id: UUID
    team_id: UUID
    api_key_id: UUID
    model_id: UUID
    model_name: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    created_at: datetime


@dataclass(frozen=True)
class UsageAggregate:
    """Usage summed for one model (over an optional api-key/model filter)."""

    model_id: UUID
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    calls: int


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
    api_key_id: UUID
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
