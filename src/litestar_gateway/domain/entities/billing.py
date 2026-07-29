"""Billing and usage tracking entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
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
    wires evaluation into settlement; enforcement above is unaffected.

    `alert_webhook_url` / `alert_email` (Plan 07 Phase 3, design doc §8) are the
    optional per-team delivery targets for fired alerts. Each is a single
    target for v1; the outbox worker resolves them per alert and dispatches
    through the matching channel(s). `alert_webhook_url` overrides the
    platform-wide webhook target; `alert_email` is delivered via the
    platform-wide SMTP server. Both stay abstract data here — no transport
    types (URLs are plain strings, SMTP config lives in `config.Settings`)."""

    id: UUID
    team_id: UUID
    limit_cost: Decimal
    window: BudgetWindow  # noqa: F821
    created_at: datetime
    thresholds: list[int] = field(default_factory=list)
    alert_webhook_url: str | None = None
    alert_email: str | None = None


class KeyBudgetMode(StrEnum):
    """What crossing a key's cap does.

    `BLOCK` refuses the call, like the team cap. `ALERT` lets it through and
    records the overrun — for the case an operator wants visibility on a key
    without the power to break its owner's workload, which is most keys most of
    the time.
    """

    BLOCK = "block"
    ALERT = "alert"


@dataclass(frozen=True)
class ApiKeyBudget:
    """A spend cap for one API key, inside its team's cap.

    Always a *sub*-limit: the team gate runs regardless, so a key limit above
    the team's is harmless (the team cap still binds) and a key limit below it
    is the point — dividing one team's budget between the things that spend it.

    Windows are the same calendar windows as the team budget, anchored by
    `domain.budget.window_start`, so "this month" means one thing across the
    system.
    """

    id: UUID
    api_key_id: UUID
    team_id: UUID
    limit_cost: Decimal
    window: BudgetWindow  # noqa: F821
    mode: KeyBudgetMode
    created_at: datetime


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
class PendingBudgetAlert:
    """Durable outbox row for one newly-fired threshold (Plan 07 Phase 1,
    design doc §4). Written alongside the `BudgetAlertState` dedup row when a
    threshold is newly crossed at settlement; a background worker (Phase 2)
    will drain this table and dispatch through configured `NotificationChannel`s,
    deleting the row on success. `spend`/`limit_cost` are captured as of the
    firing settlement so a delayed delivery still reports accurate figures
    even if spend has moved on by the time it's sent."""

    id: UUID
    team_id: UUID
    window: BudgetWindow  # noqa: F821
    period_start: datetime
    threshold: int
    spend: Decimal
    limit_cost: Decimal
    created_at: datetime


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
    cost: Decimal
    created_at: datetime
    requested_alias: str | None = None
    resolved_model_id: UUID | None = None
    canonical_model_name: str | None = None
    callable_origin: str | None = None
    source_team_id: UUID | None = None
    # True when this event settled from the response cache (Plan 04 Phase 0)
    # rather than a real provider call — cost is always 0.0 on such an event.
    cache_hit: bool = False
    # Non-token billing dimensions (Plan 13 Phase 1, design §1), persisted so the
    # ledger stays auditable: Anthropic prompt-cache write/read token counts and
    # the number of generated images. All default 0 — a plain token call records
    # them as zero, unchanged from before this plan.
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    image_count: int = 0
    # Request correlation id (Plan 11 Slice A) — see `TraceRecord.request_id`.
    request_id: str | None = None


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
    cost: Decimal
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
    cost: Decimal
    calls: int


@dataclass(frozen=True)
class UsageBucket:
    """One time-bucketed usage aggregate (Plan 10 Phase 1).

    ``bucket_start`` is the UTC-aligned start of the bucket (e.g. the top of
    the hour or midnight UTC for a day bucket) — always tz-aware UTC,
    regardless of server timezone, so bucket boundaries never shift around a
    DST transition. Every count/sum is the total over events whose
    ``created_at`` falls in ``[bucket_start, bucket_start + granularity)``.
    A bucket is only ever emitted when it has at least one matching event —
    callers wanting a dense, gap-filled series do that themselves from
    ``UsageTimeseries.start``/``end``/``granularity``.

    ``group_key`` (Plan 10 Phase 2) is ``None`` for an ungrouped series. When
    the caller passes ``group_by="model"`` to ``UsageRepository.timeseries``,
    one row is emitted per ``(bucket_start, group_key)`` pair instead of one
    per ``bucket_start`` — ``group_key`` is then the same requested-alias-or-
    canonical-model-name label `UsageResponse.from_aggregate` already uses for
    the per-model table, so a multi-series chart can be built from a single
    call instead of one request per model."""

    bucket_start: datetime
    request_count: int
    prompt_tokens: int
    completion_tokens: int
    cost: Decimal
    group_key: str | None = None


@dataclass(frozen=True)
class UsageTimeseries:
    """A bounded, time-ordered series of `UsageBucket`s for one team over
    `[start, end)` (Plan 10 Phase 1), plus the request metadata needed to
    render or gap-fill it. Not paginated: a bucketed aggregate over a bounded
    date range returns a bounded number of rows by construction, so totals
    never depend on pagination."""

    team_id: UUID
    granularity: Literal["hour", "day"]
    start: datetime
    end: datetime
    buckets: list[UsageBucket] = field(default_factory=list)


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
    cost: Decimal
    latency_ms: float
    status: str
    created_at: datetime
    # Exception class name when status == "error" (no message: keep traces secret-free).
    error_type: str | None = None
    # True when this trace observed a response-cache hit (Plan 04 Phase 0).
    cache_hit: bool = False
    # Request correlation id (Plan 11 Slice A, docs/logging.md §2): carries the
    # same opaque id the response header/log lines use, so one inference can be
    # followed end-to-end. None outside a request context (e.g. background jobs).
    request_id: str | None = None
