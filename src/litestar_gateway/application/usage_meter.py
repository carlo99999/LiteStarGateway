"""Meters the money side of an inference call.

Everything that counts tokens or dollars lives here: pre-call budget admission
(with the in-flight reservation), usage parsing/estimation, the billing write
(ledger with durable-outbox fallback), and the ok/error observability traces.
`CompletionService` orchestrates the request and delegates settlement to this
collaborator. Request-scoped like the service (it holds the request's
`UsageRepository` session); only the `InFlightSpend` it shares is process-wide.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import anyio

from litestar_gateway.domain.budget import crossed_thresholds, window_start
from litestar_gateway.domain.entities import (
    Model,
    ModelType,
    PendingBudgetAlert,
    TraceRecord,
    UsageAttribution,
    UsageEvent,
)
from litestar_gateway.domain.exceptions import BudgetExceeded, RateLimited
from litestar_gateway.domain.money import ZERO
from litestar_gateway.domain.ports import (
    APIKeyRepository,
    BudgetAlertStateRepository,
    BudgetRepository,
    BudgetReservationStore,
    RateLimiter,
    Reservation,
    TeamRepository,
    UsageRepository,
)
from litestar_gateway.domain.pricing import (
    BillableUsage,
    RateCard,
    compute_cost,
    rate_card,
)
from litestar_gateway.request_context import current_request_id

# How long an unsettled reservation holds a team's headroom before the store
# reclaims it. Long enough for a slow streamed completion, short enough that a
# replica killed mid-request does not strand budget until someone notices.
DEFAULT_RESERVATION_TTL_SECONDS = 300

logger = logging.getLogger("litestar_gateway.usage")

# Coarse industry heuristic, used only when no authoritative usage arrives
# (client disconnect mid-stream, or a provider stream that never reports it).
_CHARS_PER_TOKEN = 4


def _estimate_tokens(chars: int) -> int:
    return (chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _request_text(request: dict[str, Any]) -> str:
    """Concatenated prompt text of a chat or Responses request, for estimation."""
    parts: list[str] = []
    if isinstance(request.get("instructions"), str):
        parts.append(request["instructions"])
    # Anthropic-native bodies carry the system prompt in a top-level `system`
    # field (string or list of content blocks), outside `messages` (R8-ISSUE-008).
    system = request.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    value = request.get("input")
    if isinstance(value, str):
        parts.append(value)
    items = request.get("messages") or (value if isinstance(value, list) else [])
    for item in items:
        if isinstance(item, str):  # embeddings input may be a list[str]
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        for field in ("call_id", "name", "arguments", "output"):
            field_value = item.get(field)
            if isinstance(field_value, str):
                parts.append(field_value)
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            parts.append(_serialized_prompt_value(tool_calls))
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and isinstance(c.get("text"), str)
            )
    for field in ("tools", "tool_choice", "response_format", "text"):
        if request.get(field) is not None:
            parts.append(_serialized_prompt_value(request[field]))
    return "\n".join(parts)


def _serialized_prompt_value(value: Any) -> str:
    """Stable text approximation for JSON-shaped prompt metadata."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except TypeError, ValueError:
        return ""


def _chunk_output_text(chunk: dict[str, Any]) -> str:
    """Output text carried by one stream chunk (chat delta or Responses event).

    Sums the delta across *all* choices, not just `choices[0]`: an `n>1` chat
    stream that disconnects or errors before the authoritative usage chunk would
    otherwise have its estimate under-count the other n-1 choices (L19)."""
    if chunk.get("type") in (
        "response.output_text.delta",
        "response.function_call_arguments.delta",
    ):
        delta = chunk.get("delta")
        return delta if isinstance(delta, str) else ""
    text = ""
    for choice in chunk.get("choices") or []:
        if isinstance(choice, dict):
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str):
                text += content
    return text


def _native_event_text(event: dict[str, Any]) -> str:
    """Output text carried by one raw Anthropic stream event, for the estimation
    fallback when a disconnect arrives before any authoritative usage. Native
    text lands on `content_block_delta` events as `text_delta`/`input_json_delta`;
    everything else contributes no output text."""
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    for key in ("text", "partial_json"):
        value = delta.get(key)
        if isinstance(value, str):
            return value
    return ""


def _gemini_chunk_text(chunk: dict[str, Any]) -> str:
    """Output text carried by one raw Gemini `GenerateContentResponse` chunk, for
    the estimation fallback when a disconnect arrives before any authoritative
    usage. Text lands on `candidates[].content.parts[].text`; everything else
    contributes no output text."""
    text = ""
    for candidate in chunk.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for part in (candidate.get("content") or {}).get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text += part["text"]
    return text


def _has_tokens(usage: dict[str, Any]) -> bool:
    return any(
        int(usage.get(key) or 0)
        for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
    )


def _has_authoritative_usage(usage: dict[str, Any]) -> bool:
    """Whether a provider explicitly reported a complete, non-negative token pair.

    Unlike `_has_tokens`, an authoritative all-zero pair is meaningful: for
    example, Anthropic pre-output refusals are successful but uncharged and must
    not fall back to a request-size billing estimate.
    """
    for prompt_key, completion_key in (
        ("prompt_tokens", "completion_tokens"),
        ("input_tokens", "output_tokens"),
    ):
        prompt = usage.get(prompt_key)
        completion = usage.get(completion_key)
        if (
            isinstance(prompt, int)
            and not isinstance(prompt, bool)
            and prompt >= 0
            and isinstance(completion, int)
            and not isinstance(completion, bool)
            and completion >= 0
        ):
            return True
    return False


def _is_uncharged_refusal(response: dict[str, Any], usage: dict[str, Any]) -> bool:
    chat_refusal = any(
        isinstance(choice, dict) and choice.get("finish_reason") == "content_filter"
        for choice in response.get("choices") or []
    )
    responses_refusal = (
        response.get("status") == "incomplete"
        and (response.get("incomplete_details") or {}).get("reason") == "content_filter"
    )
    return (
        (chat_refusal or responses_refusal)
        and _has_authoritative_usage(usage)
        and not _has_tokens(usage)
    )


def _max_output_tokens(request: dict[str, Any]) -> int:
    # Chat uses max_tokens (legacy) / max_completion_tokens; Responses uses
    # max_output_tokens. First positive one wins.
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = request.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def _rate_card(model: Model) -> RateCard:
    """The model's pricing inputs as the pure `RateCard` the normalized pricing
    function consumes. Isolating the `Model → RateCard` projection here keeps
    `domain.pricing` free of the `Model` entity — and is where the model's
    still-`float` columns become exact `Decimal` rates, once, rather than at each
    call site."""
    return rate_card(
        input_cost_per_token=model.input_cost_per_token,
        output_cost_per_token=model.output_cost_per_token,
        cache_write_cost_per_token=model.cache_write_cost_per_token,
        cache_read_cost_per_token=model.cache_read_cost_per_token,
        image_cost_per_image=model.image_cost_per_image,
        image_prices=model.image_prices,
    )


def _token_usage(usage: dict[str, Any]) -> BillableUsage:
    """Normalize a provider usage dict into `BillableUsage` token quantities.
    Chat completions report prompt/completion_tokens; the Responses API reports
    input/output_tokens — bill either shape. Anthropic additionally reports
    `cache_creation_input_tokens`/`cache_read_input_tokens`, kept as distinct
    dimensions (design §1). Explicit key-presence checks (not `or`-chaining) so a
    legitimate 0 is never overridden."""
    if "prompt_tokens" in usage:
        prompt = int(usage.get("prompt_tokens") or 0)
    else:
        prompt = int(usage.get("input_tokens") or 0)
    if "completion_tokens" in usage:
        completion = int(usage.get("completion_tokens") or 0)
    else:
        completion = int(usage.get("output_tokens") or 0)
    return BillableUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
    )


def _image_usage(request: dict[str, Any] | None, response: dict[str, Any]) -> BillableUsage:
    """Normalize an image-generation call into `BillableUsage`: the authoritative
    image count is how many images the response actually returned (`data`), priced
    by the request's size/quality. An errored image call has no `data`, so it bills
    zero — the requested-`n` upper bound was already reserved at admission."""
    data = response.get("data")
    count = len(data) if isinstance(data, list) else 0
    size = request.get("size") if request else None
    quality = request.get("quality") if request else None
    return BillableUsage(
        image_count=count,
        image_size=size if isinstance(size, str) else None,
        image_quality=quality if isinstance(quality, str) else None,
    )


def _parse_usage(model: Model, usage: dict[str, Any]) -> tuple[int, int, Decimal]:
    """Token counts + cost from a provider usage dict, via the one normalized
    pricing function (design §1). Returned as a `(prompt, completion, cost)` tuple
    for the token settlement/error paths; `_token_usage` exposes the full
    `BillableUsage` (incl. cache tokens) that `_bill` persists."""
    billable = _token_usage(usage)
    cost = compute_cost(billable, _rate_card(model))
    return billable.prompt_tokens, billable.completion_tokens, cost


def _worst_case_prompt_usage(
    prompt_estimate: int, output_estimate: int, rates: RateCard
) -> BillableUsage:
    """Assign the whole estimated prompt to the priciest input-side bucket
    (ordinary input vs. cache-write vs. cache-read). Settlement later splits the
    real prompt across those buckets, each priced at or below this max, so the
    reservation can never under-estimate the eventual charge — the budget-gate
    upper-bound invariant, preserved once cache tokens enter the picture. With no
    cache rates configured the max is the ordinary input rate, so the reservation
    is byte-identical to the pre-Plan-13 formula."""
    input_rate = rates.input_cost_per_token or 0.0
    cache_write_rate = rates.cache_write_cost_per_token or 0.0
    cache_read_rate = rates.cache_read_cost_per_token or 0.0
    if cache_write_rate > input_rate and cache_write_rate >= cache_read_rate:
        return BillableUsage(cache_write_tokens=prompt_estimate, completion_tokens=output_estimate)
    if cache_read_rate > input_rate:
        return BillableUsage(cache_read_tokens=prompt_estimate, completion_tokens=output_estimate)
    return BillableUsage(prompt_tokens=prompt_estimate, completion_tokens=output_estimate)


def _reservation_cost(model: Model, request: dict[str, Any]) -> Decimal:
    """Pessimistic pre-dispatch cost of a request, via the same normalized pricing
    function settlement uses. For image models: the requested image count at the
    request's size/quality (an upper bound — settlement bills the images actually
    returned). For token models: the estimated prompt plus the requested output
    ceiling per choice — `n` choices each regenerate the full output ceiling
    (providers bill the prompt once). Callers pass the sanitized request, so
    `n`/max-tokens are already clamped."""
    rates = _rate_card(model)
    n = request.get("n")
    choices = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
    if model.type is ModelType.IMAGE:
        size = request.get("size")
        quality = request.get("quality")
        usage = BillableUsage(
            image_count=choices,
            image_size=size if isinstance(size, str) else None,
            image_quality=quality if isinstance(quality, str) else None,
        )
        return compute_cost(usage, rates)
    prompt_estimate = _estimate_tokens(len(_request_text(request)))
    output_estimate = _max_output_tokens(request) * choices
    return compute_cost(_worst_case_prompt_usage(prompt_estimate, output_estimate, rates), rates)


class UsageMeter:
    """Admission, settlement, and tracing for one request's spend."""

    def __init__(
        self,
        usage: UsageRepository,
        emit_trace: Callable[[TraceRecord], None],
        budgets: BudgetRepository | None = None,
        reservations: BudgetReservationStore | None = None,
        reservation_ttl_s: int = DEFAULT_RESERVATION_TTL_SECONDS,
        settlement_timeout: float = 30.0,
        rate_limiter: RateLimiter | None = None,
        teams: TeamRepository | None = None,
        api_keys: APIKeyRepository | None = None,
        budget_alert_state: BudgetAlertStateRepository | None = None,
    ) -> None:
        self._usage = usage
        self._emit_trace = emit_trace
        self._budgets = budgets
        # Proactive threshold alerts (Plan 07 Phase 1): optional, like `budgets`
        # itself — without both a budget repo and this dedup/outbox port,
        # settle_ok's alert evaluation is a no-op.
        self._budget_alert_state = budget_alert_state
        # Rate limiting is opt-in: without a limiter admit skips the RPM gates.
        # The team gate needs the team repo; the key gate needs the api-key repo.
        self._rate_limiter = rate_limiter
        self._teams = teams
        self._api_keys = api_keys
        # Library use may omit it; the web wiring passes one shared instance so
        # request-scoped meters see each other's reservations.
        self._reservations = reservations
        self._reservation_ttl_s = reservation_ttl_s
        self._pending_releases: set[asyncio.Task[None]] = set()
        # Upper bound on the shielded stream settlement, so a stalled DB can't
        # leave an unbounded pile of orphan cleanup coroutines (M29).
        self._settlement_timeout = settlement_timeout

    async def admit(
        self,
        team_id: UUID,
        model: Model,
        request: dict[str, Any],
        *,
        api_key_id: UUID | None = None,
        skip_team_rate_limit: bool = False,
    ) -> Reservation | None:
        """Pre-call spend gate: reject once committed spend plus the estimated
        cost already reserved by in-flight requests reaches the budget limit.
        An admitted request immediately reserves its own pessimistic cost
        (prompt estimate + requested output ceiling) and returns the claim —
        callers release it at settlement. Without the reservation, any number
        of concurrent requests could pass the gate before the first one settles
        (streams widen that blind spot to minutes).

        The bound is fleet-wide, not per replica: the store decides and records
        in one atomic step, so two replicas cannot both read the same in-flight
        total and both slip under the cap. `None` means there was nothing to
        gate — no budget configured, or no store wired.

        `skip_team_rate_limit` is for cross-provider failover retries only
        (Plan 05): one logical client request must consume exactly one
        team-RPM hit, taken on the first attempt. A retry against the next
        candidate still needs its own fresh budget reservation (a real
        provider call is about to happen), but re-checking the team's
        requests-per-minute gate on every retry would silently multiply one
        request into N rate-limit consumptions."""
        if not skip_team_rate_limit:
            await self._enforce_team_rate_limit(team_id)
        await self.enforce_key_rate_limit(api_key_id)
        if self._budgets is None:
            return None
        budget = await self._budgets.get(team_id)
        if budget is None:
            return None
        since = window_start(budget.window, datetime.now(UTC))
        spent = await self._usage.spend_since(team_id, since)
        if self._reservations is None:
            # No store wired: the cap still holds on committed spend, but
            # nothing bounds a concurrent burst. Unreachable in production —
            # Redis is mandatory and the store is always wired — this is for
            # library use and for tests that do not exercise admission.
            if spent >= budget.limit_cost:
                raise BudgetExceeded(
                    f"Team budget exceeded: spent {spent:.4f} of {budget.limit_cost:.4f} USD "
                    f"in the current {budget.window} window"
                )
            return None
        # Read the in-flight total, decide and record as ONE step in the store:
        # doing it here in two awaits is what would let two admissions see the
        # same total and both slip through.
        outcome = await self._reservations.try_reserve(
            team_id,
            # The store's arithmetic is a comparison against the budget, both
            # still floats; PR 2/3 of this slice migrates the columns and this
            # conversion goes away.
            # The reservation store still compares floats; PR 3/3 of this
            # slice decimalizes it together with the rate columns.
            float(_reservation_cost(model, request)),
            spent=float(spent),
            limit=float(budget.limit_cost),
            ttl_s=self._reservation_ttl_s,
        )
        if not outcome.admitted:
            raise BudgetExceeded(
                f"Team budget exceeded: spent {spent:.4f} (+{outcome.reserved:.4f} USD reserved "
                f"by in-flight requests) of {budget.limit_cost:.4f} USD "
                f"in the current {budget.window} window"
            )
        return outcome.reservation

    async def _enforce_team_rate_limit(self, team_id: UUID) -> None:
        """Per-team requests/minute gate, checked before the budget reservation so
        a throttled request never reserves spend. No-op unless a rate limiter and
        team repo are wired and the team has a limit set (RateLimited → 429)."""
        if self._rate_limiter is None or self._teams is None:
            return
        team = await self._teams.get(team_id)
        if team is None or team.rate_limit_rpm is None:
            return
        decision = await self._rate_limiter.hit(f"team:{team_id}", team.rate_limit_rpm)
        if not decision.allowed:
            raise RateLimited(
                f"Team rate limit exceeded: {team.rate_limit_rpm} requests/min",
                retry_after=decision.retry_after,
            )

    async def enforce_key_rate_limit(self, api_key_id: UUID | None) -> None:
        """Per-key requests/minute gate. No-op for internal calls with no user key
        (e.g. router judge/embeddings strategies) or when unwired / no limit set."""
        if self._rate_limiter is None or self._api_keys is None or api_key_id is None:
            return
        key = await self._api_keys.get(api_key_id)
        if key is None or key.rate_limit_rpm is None:
            return
        decision = await self._rate_limiter.hit(f"key:{api_key_id}", key.rate_limit_rpm)
        if not decision.allowed:
            raise RateLimited(
                f"API key rate limit exceeded: {key.rate_limit_rpm} requests/min",
                retry_after=decision.retry_after,
            )

    async def release(self, reservation: Reservation | None) -> None:
        """Give back a reservation taken at admission (settlement or failure).
        Idempotent — releasing the same claim twice deletes nothing the second
        time — so callers may release defensively."""
        if reservation is None or self._reservations is None:
            return
        await self._reservations.release(reservation)

    def release_soon(self, reservation: Reservation | None) -> None:
        """Release from a context that cannot await — specifically the
        `weakref.finalize` guarding a stream the client dropped before its first
        byte, which runs as a garbage-collection callback.

        Best effort by construction: with a running loop the release is
        scheduled and lands within the tick; without one (interpreter shutdown,
        a GC pass off the loop thread) nothing happens and the reservation's TTL
        reclaims it. That bounded delay is the price of not blocking a GC
        callback, and it is strictly better than the previous behaviour on a
        crashed replica, which lost the in-process counter entirely."""
        if reservation is None or self._reservations is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "no running loop to release reservation %s; its TTL will reclaim it",
                reservation.id,
            )
            return
        task = loop.create_task(self.release(reservation))
        # Hold a reference: a task with no strong reference can be garbage
        # collected mid-flight, which would silently skip the release.
        self._pending_releases.add(task)
        task.add_done_callback(self._pending_releases.discard)

    async def settle_ok(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        response: dict[str, Any],
        latency_ms: float,
        request: dict[str, Any] | None = None,
        attribution: UsageAttribution | None = None,
    ) -> tuple[int, int, Decimal]:
        """Record usage (billing) + emit an observability trace. Fail-safe.

        If the provider reported no usable token counts (e.g. an adapter that
        omits usage), estimate the prompt from the request rather than billing
        zero silently — the non-streaming mirror of the stream estimate (H14).

        Returns the settled `(prompt_tokens, completion_tokens, cost)` — the
        exact counts written to the ledger — so a caller with its own
        secondary bookkeeping (e.g. a stream's routing-decision usage
        attachment, Plan 10 Phase 0) can reuse them instead of re-deriving
        usage from the response body."""
        prompt, completion, cost, now = await self._settle_usage(
            team_id,
            api_key_id,
            model,
            operation,
            response,
            request,
            attribution,
        )
        # Proactive threshold alerts (Plan 07 Phase 1, design doc §3): evaluated
        # right after the ledger write, on committed spend. Not hooked into
        # settle_error/settle_cache_hit — only a genuine successful settlement
        # advances alerts, matching the plan's scope.
        await self._evaluate_budget_alerts(team_id, now)
        # Trace = observability (latency/analytics), fire-and-forget off the path.
        self._emit_trace(
            TraceRecord(
                team_id=team_id,
                api_key_id=api_key_id,
                model_name=model.name,
                provider=model.provider.value,
                operation=operation,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost=cost,
                latency_ms=latency_ms,
                status="ok",
                created_at=now,
                request_id=current_request_id(),
            )
        )
        return prompt, completion, cost

    async def settle_cache_hit(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        attribution: UsageAttribution | None = None,
    ) -> None:
        """Settle a response-cache hit (Plan 04 Phase 0, design §6): bill the
        *stored* token counts at a hard `cost=0.0` rather than routing through
        `_parse_usage` (which would compute a real, non-zero cost from those
        same counts) — never double-bill or under-bill a hit. Still flows
        through the ledger + trace so usage stays attributable; both records
        carry `cache_hit=True`. The budget reservation taken at admission is
        released by the caller (`CompletionService._dispatch`), same as every
        other settlement path."""
        now = datetime.now(UTC)
        await self._bill(
            team_id,
            api_key_id,
            model,
            operation,
            BillableUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
            ZERO,
            now,
            attribution,
            cache_hit=True,
        )
        self._emit_trace(
            TraceRecord(
                team_id=team_id,
                api_key_id=api_key_id,
                model_name=model.name,
                provider=model.provider.value,
                operation=operation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=ZERO,
                latency_ms=latency_ms,
                status="ok",
                created_at=now,
                cache_hit=True,
                request_id=current_request_id(),
            )
        )

    async def settle_error(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        response: dict[str, Any],
        latency_ms: float,
        exc: BaseException,
        request: dict[str, Any] | None = None,
        attribution: UsageAttribution | None = None,
    ) -> tuple[int, int, Decimal]:
        """Bill a completed provider invocation whose response is unusable."""
        prompt, completion, cost, _ = await self._settle_usage(
            team_id,
            api_key_id,
            model,
            operation,
            response,
            request,
            attribution,
        )
        self.trace_error(
            team_id,
            api_key_id,
            model,
            operation,
            latency_ms,
            exc,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost=cost,
        )
        return prompt, completion, cost

    async def _settle_usage(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        response: dict[str, Any],
        request: dict[str, Any] | None,
        attribution: UsageAttribution | None,
    ) -> tuple[int, int, Decimal, datetime]:
        if model.type is ModelType.IMAGE:
            # Image responses carry no token usage — bill on the image count/size/
            # quality dimensions instead of estimating a (meaningless) prompt.
            billable = _image_usage(request, response)
        else:
            usage = response.get("usage") or {}
            uncharged_refusal = _is_uncharged_refusal(response, usage)
            if not _has_tokens(usage) and not uncharged_refusal and request is not None:
                estimate = {"prompt_tokens": _estimate_tokens(len(_request_text(request)))}
                if _has_tokens(estimate):
                    usage = estimate
                    logger.warning(
                        "no authoritative usage from provider; billing estimate: "
                        "team=%s model=%s op=%s prompt=%s",
                        team_id,
                        model.name,
                        operation,
                        usage["prompt_tokens"],
                    )
            billable = _token_usage(usage)
        cost = compute_cost(billable, _rate_card(model))
        now = datetime.now(UTC)
        await self._bill(team_id, api_key_id, model, operation, billable, cost, now, attribution)
        return billable.prompt_tokens, billable.completion_tokens, cost, now

    def trace_error(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        latency_ms: float,
        exc: BaseException,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: Decimal = ZERO,
    ) -> None:
        """Emit a status='error' trace for a failed gateway call. Without this,
        provider outages/timeouts/rate-limits are invisible in tracing — exactly
        the events operators most need to see. Non-stream failures carry zero
        usage (the provider reported none); a mid-stream failure passes the
        usage billed for what streamed before the error."""
        self._emit_trace(
            TraceRecord(
                team_id=team_id,
                api_key_id=api_key_id,
                model_name=model.name,
                provider=model.provider.value,
                operation=operation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
                latency_ms=latency_ms,
                status="error",
                created_at=datetime.now(UTC),
                error_type=type(exc).__name__,
                request_id=current_request_id(),
            )
        )

    async def _record_usage(self, event: UsageEvent) -> None:
        """Persist the billing record. A failed write must never fail the request,
        but it must not vanish either: on failure the event is dead-lettered to a
        durable outbox and retried by the background reconciler. Only if that also
        fails do we fall back to an ERROR log with the full event (no secrets).

        Guarantee level: at-most-once on crash. The outbox is a dead-letter for
        *failed* writes, not a write-ahead intent — a process kill between the
        upstream response and this call leaves no durable artifact of the spend.
        Closing that window would take a pre-dispatch intent row reconciled at
        settlement; accepted as out of scope for now."""
        try:
            await self._usage.record(event)
            return
        except Exception:  # recording must not fail the request
            logger.warning("usage record failed; dead-lettering to outbox", exc_info=True)
        try:
            await self._usage.enqueue_pending(event)
        except Exception:
            logger.error(
                "usage event dropped (record + outbox failed): "
                "team=%s api_key=%s model=%s op=%s prompt=%s completion=%s cost=%s at=%s",
                event.team_id,
                event.api_key_id,
                event.model_name,
                event.operation,
                event.prompt_tokens,
                event.completion_tokens,
                event.cost,
                event.created_at.isoformat(),
                exc_info=True,
            )

    async def _bill(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        usage: BillableUsage,
        cost: Decimal,
        now: datetime,
        attribution: UsageAttribution | None = None,
        *,
        cache_hit: bool = False,
    ) -> None:
        """Persist the authoritative billing record (no trace — callers emit
        their own 'ok' or 'error' trace alongside). Records every billable
        dimension of the normalized usage so the ledger stays auditable."""
        await self._record_usage(
            UsageEvent(
                id=uuid4(),
                team_id=team_id,
                api_key_id=api_key_id,
                model_id=model.id,
                model_name=model.name,
                operation=operation,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost=cost,
                created_at=now,
                request_id=current_request_id(),
                requested_alias=attribution.requested_alias if attribution else None,
                resolved_model_id=model.id,
                canonical_model_name=model.name,
                callable_origin=attribution.callable_origin if attribution else None,
                source_team_id=attribution.source_team_id if attribution else None,
                cache_hit=cache_hit,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                image_count=usage.image_count,
            )
        )

    async def _evaluate_budget_alerts(self, team_id: UUID, now: datetime) -> None:
        """Proactive threshold alerts (Plan 07 Phase 1, design doc §3). Reads
        the same `spend_since` aggregate the pre-call gate uses, scoped to the
        budget's own window, so a threshold newly crossed by this settlement's
        committed cost is caught. For each newly-crossed threshold: record the
        dedup key first, and only enqueue an outbox row if that insert actually
        won the race (a `None` return means a concurrent settlement already
        fired it) — this ordering is what makes "fired but never enqueued"
        impossible while still tolerating "enqueued but not yet delivered".

        Optional like the budget gate itself: without both a `BudgetRepository`
        and a `BudgetAlertStateRepository` wired, or without any configured
        thresholds, this is a no-op. Fail-safe: any error here is logged and
        swallowed, never widening `settle_ok`'s own failure surface (design
        doc §7) — a broken alert evaluation must never fail a billed request."""
        if self._budgets is None or self._budget_alert_state is None:
            return
        try:
            budget = await self._budgets.get(team_id)
            if budget is None or not budget.thresholds:
                return
            period_start = window_start(budget.window, now)
            spend = await self._usage.spend_since(team_id, period_start)
            fired = await self._budget_alert_state.fired_thresholds(
                team_id, budget.window, period_start
            )
            newly_crossed = crossed_thresholds(
                spend=spend,
                limit_cost=budget.limit_cost,
                thresholds=budget.thresholds,
                fired=fired,
            )
            for threshold in newly_crossed:
                await self._budget_alert_state.record_fired_and_enqueue(
                    PendingBudgetAlert(
                        id=uuid4(),
                        team_id=team_id,
                        window=budget.window,
                        period_start=period_start,
                        threshold=threshold,
                        spend=spend,
                        limit_cost=budget.limit_cost,
                        created_at=now,
                    )
                )
        except Exception:  # alert evaluation must never fail the request
            logger.warning("budget alert evaluation failed", exc_info=True)

    @staticmethod
    async def _notify_settled(
        on_settled: Callable[[int, int], Awaitable[None]] | None,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Fail-safe invocation of a stream's settlement callback (Plan 10
        Phase 0): a bug in the caller's secondary bookkeeping (e.g. attaching
        usage to a routing decision) must never break billing, which has
        already been durably written by the time this runs, or the SSE
        response already in flight."""
        if on_settled is None:
            return
        try:
            await on_settled(prompt_tokens, completion_tokens)
        except Exception:
            logger.warning("stream settlement callback failed", exc_info=True)

    async def metered_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        release: Callable[[], Awaitable[None]] | None = None,
        attribution: UsageAttribution | None = None,
        on_settled: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Relay chunks unchanged while capturing usage as it flows, then record a
        UsageEvent + emit a trace once the stream finishes (or the client
        disconnects — the `finally` runs on generator close). Without this,
        streamed calls were neither billed nor observed. A provider error
        mid-stream still bills what streamed before the failure (those tokens
        were paid upstream) but emits a status='error' trace instead of a fake
        'ok' one (a client disconnect records as 'ok': bill what was seen).
        A real disconnect arrives as scope cancellation at the provider await,
        so the settlement is shielded — otherwise its first checkpoint would
        re-raise CancelledError and the billing write would silently vanish.
        If no authoritative usage arrived by then (disconnect
        before the usage chunk, or a provider that never reported one), usage
        is estimated from the request text + streamed output rather than
        silently billed as zero."""
        start = perf_counter()
        usage: dict[str, Any] = {}
        streamed_chars = 0
        error: Exception | None = None
        refusal_seen = False
        authoritative_zero = False
        try:
            async for chunk in stream:
                # OpenAI chat puts usage at the top level (final chunk); the
                # Responses API nests it under `response`.
                found = chunk.get("usage") or (chunk.get("response") or {}).get("usage")
                if found:
                    usage = found
                refusal_seen = (
                    refusal_seen
                    or any(
                        isinstance(choice, dict) and choice.get("finish_reason") == "content_filter"
                        for choice in chunk.get("choices") or []
                    )
                    or (
                        chunk.get("type") == "response.incomplete"
                        and (chunk.get("response") or {})
                        .get("incomplete_details", {})
                        .get("reason")
                        == "content_filter"
                    )
                )
                authoritative_zero = (
                    refusal_seen and _has_authoritative_usage(usage) and not _has_tokens(usage)
                )
                streamed_chars += len(_chunk_output_text(chunk))
                yield chunk
        except Exception as exc:
            error = exc
            raise
        finally:
            # Shielded: on a client disconnect this frame is already cancelled,
            # and the first checkpoint — now the release, then the DB commit —
            # would re-raise CancelledError, leaving the reservation held and
            # no ledger row, outbox or trace behind. Releasing first inside the
            # shield keeps the old ordering now that the release awaits.
            # release() is idempotent: the caller also finalizes it for the
            # never-iterated case (M27), so a double call here is safe.
            with anyio.CancelScope(shield=True):
                if release is not None:
                    await release()
                await self._finalize_stream_billing(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    request,
                    usage,
                    streamed_chars,
                    error,
                    start,
                    attribution,
                    authoritative_zero=authoritative_zero,
                    on_settled=on_settled,
                )

    async def metered_native_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        release: Callable[[], Awaitable[None]] | None = None,
        attribution: UsageAttribution | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Relay raw Anthropic Messages stream events unchanged while capturing the
        native usage they carry, then settle once at the tail (or on client
        disconnect — the shielded finally). This is the native mirror of
        `metered_stream`: identical release-once + shielded-settlement machinery,
        but usage is read from the Anthropic event shape rather than the OpenAI/
        Responses shapes. `message_start` reports `message.usage.input_tokens`;
        `message_delta` reports the running top-level `usage.output_tokens`. The
        accumulated `{input_tokens, output_tokens}` settle through the same
        `_finalize_stream_billing` path (estimation fallback, error trace,
        settlement timeout) as every other stream."""
        start = perf_counter()
        usage: dict[str, Any] = {}
        streamed_chars = 0
        error: Exception | None = None
        try:
            async for event in stream:
                etype = event.get("type")
                if etype == "message_start":
                    start_usage = (event.get("message") or {}).get("usage") or {}
                    if "input_tokens" in start_usage:
                        usage["input_tokens"] = start_usage.get("input_tokens") or 0
                    if "output_tokens" in start_usage:
                        usage["output_tokens"] = start_usage.get("output_tokens") or 0
                    # Prompt-cache tokens are reported once, on message_start
                    # (Plan 13 Phase 1); settle them at their own rates.
                    for cache_key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                        if cache_key in start_usage:
                            usage[cache_key] = start_usage.get(cache_key) or 0
                elif etype == "message_delta":
                    delta_usage = event.get("usage") or {}
                    if "output_tokens" in delta_usage:
                        usage["output_tokens"] = delta_usage.get("output_tokens") or 0
                streamed_chars += len(_native_event_text(event))
                yield event
        except Exception as exc:
            error = exc
            raise
        finally:
            # Release first, inside the shield so a cancelled scope cannot
            # re-raise at the release's checkpoint — same ordering and
            # guarantees as metered_stream.
            with anyio.CancelScope(shield=True):
                if release is not None:
                    await release()
                await self._finalize_stream_billing(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    request,
                    usage,
                    streamed_chars,
                    error,
                    start,
                    attribution,
                )

    async def metered_gemini_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        release: Callable[[], Awaitable[None]] | None = None,
        attribution: UsageAttribution | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Relay raw Gemini `GenerateContentResponse` chunks unchanged while
        capturing the native `usageMetadata` they carry, then settle once at the
        tail (or on client disconnect — the shielded finally). This is the native
        mirror of `metered_native_stream`: identical release-once + shielded-
        settlement machinery, but usage is read from the Gemini wire shape
        (`usageMetadata.promptTokenCount` / `candidatesTokenCount`, reported
        cumulatively — the final chunk carries the totals). The accumulated
        `{input_tokens, output_tokens}` settle through the same
        `_finalize_stream_billing` path (estimation fallback, error trace,
        settlement timeout) as every other stream."""
        start = perf_counter()
        usage: dict[str, Any] = {}
        streamed_chars = 0
        error: Exception | None = None
        try:
            async for chunk in stream:
                meta = chunk.get("usageMetadata")
                if meta:
                    if "promptTokenCount" in meta:
                        usage["input_tokens"] = meta.get("promptTokenCount") or 0
                    if "candidatesTokenCount" in meta:
                        usage["output_tokens"] = meta.get("candidatesTokenCount") or 0
                streamed_chars += len(_gemini_chunk_text(chunk))
                yield chunk
        except Exception as exc:
            error = exc
            raise
        finally:
            # Release first, inside the shield so a cancelled scope cannot
            # re-raise at the release's checkpoint — same ordering and
            # guarantees as metered_stream.
            with anyio.CancelScope(shield=True):
                if release is not None:
                    await release()
                await self._finalize_stream_billing(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    request,
                    usage,
                    streamed_chars,
                    error,
                    start,
                    attribution,
                )

    async def _finalize_stream_billing(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        request: dict[str, Any],
        usage: dict[str, Any],
        streamed_chars: int,
        error: Exception | None,
        start: float,
        attribution: UsageAttribution | None,
        *,
        authoritative_zero: bool = False,
        on_settled: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        """Post-stream settlement: estimate usage if none arrived, bill, and
        trace. Runs inside `metered_stream`'s shielded finally — callers must
        already hold the cancellation shield.

        `on_settled` (Plan 10 Phase 0) is an optional callback invoked with the
        exact `(prompt_tokens, completion_tokens)` pair once they are actually
        billed — real counts on normal completion, the same partial/estimated
        counts the ledger receives on a mid-stream error or client disconnect.
        It is never invoked for the zero-consumption case (nothing was billed,
        M26). Failure to notify must never fail settlement or the client
        stream: `_notify_settled` swallows and logs any exception the callback
        raises, mirroring `RouterService.record_usage`'s own fail-safe guard —
        two independent safety nets around the same secondary write."""
        latency_ms = (perf_counter() - start) * 1000
        # A provider that rejects the request before emitting anything
        # (error, zero streamed output, no usage reported) consumed
        # nothing upstream — don't fabricate a prompt estimate to bill it
        # (M26). A client disconnect (no error) still estimates and bills:
        # there the provider did consume the prompt. A mid-stream failure
        # after some output also bills — those tokens were paid upstream.
        produced_nothing = error is not None and streamed_chars == 0 and not _has_tokens(usage)
        if not _has_tokens(usage) and not produced_nothing and not authoritative_zero:
            estimate = {
                "prompt_tokens": _estimate_tokens(len(_request_text(request))),
                "completion_tokens": _estimate_tokens(streamed_chars),
            }
            if _has_tokens(estimate):
                usage = estimate
                logger.warning(
                    "stream ended without authoritative usage; billing estimate: "
                    "team=%s model=%s op=%s prompt=%s completion=%s",
                    team_id,
                    model.name,
                    operation,
                    usage["prompt_tokens"],
                    usage["completion_tokens"],
                )
        # Bound the DB settlement: the caller's shield (correctly) makes it
        # uncancellable by a client disconnect, but without a deadline a
        # stalled DB would leave this coroutine — and its pool connection
        # — orphaned forever, piling up under degradation and hanging
        # graceful shutdown (M29). On timeout the spend for this one
        # settlement is dropped with an ERROR (a Postgres statement
        # timeout would instead surface as a failure the outbox catches).
        with anyio.move_on_after(self._settlement_timeout) as settle_scope:
            if error is not None:
                # Bill what was seen (nothing, if the provider produced
                # nothing), but keep the honest error trace instead of a
                # fake 'ok' one — carrying the billed usage.
                billable = _token_usage(usage)
                prompt, completion = billable.prompt_tokens, billable.completion_tokens
                cost = compute_cost(billable, _rate_card(model))
                if _has_tokens(usage):
                    await self._bill(
                        team_id,
                        api_key_id,
                        model,
                        operation,
                        billable,
                        cost,
                        datetime.now(UTC),
                        attribution,
                    )
                    await self._notify_settled(on_settled, prompt, completion)
                self.trace_error(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    latency_ms,
                    error,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    cost=cost,
                )
            else:
                prompt, completion, _cost = await self.settle_ok(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    {"usage": usage},
                    latency_ms,
                    attribution=attribution,
                )
                await self._notify_settled(on_settled, prompt, completion)
        if settle_scope.cancelled_caught:
            logger.error(
                "stream settlement timed out after %ss; spend may be unrecorded: "
                "team=%s model=%s op=%s",
                self._settlement_timeout,
                team_id,
                model.name,
                operation,
            )
