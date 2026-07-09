"""Meters the money side of an inference call.

Everything that counts tokens or dollars lives here: pre-call budget admission
(with the in-flight reservation), usage parsing/estimation, the billing write
(ledger with durable-outbox fallback), and the ok/error observability traces.
`CompletionService` orchestrates the request and delegates settlement to this
collaborator. Request-scoped like the service (it holds the request's
`UsageRepository` session); only the `InFlightSpend` it shares is process-wide.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import anyio

from litestar_gateway.domain.budget import window_start
from litestar_gateway.domain.entities import Model, TraceRecord, UsageEvent
from litestar_gateway.domain.exceptions import BudgetExceeded
from litestar_gateway.domain.ports import BudgetRepository, UsageRepository

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
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and isinstance(c.get("text"), str)
            )
    return "\n".join(parts)


def _chunk_output_text(chunk: dict[str, Any]) -> str:
    """Output text carried by one stream chunk (chat delta or Responses event).

    Sums the delta across *all* choices, not just `choices[0]`: an `n>1` chat
    stream that disconnects or errors before the authoritative usage chunk would
    otherwise have its estimate under-count the other n-1 choices (L19)."""
    if chunk.get("type") == "response.output_text.delta":
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


def _max_output_tokens(request: dict[str, Any]) -> int:
    # Chat uses max_tokens (legacy) / max_completion_tokens; Responses uses
    # max_output_tokens. First positive one wins.
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = request.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def _parse_usage(model: Model, usage: dict[str, Any]) -> tuple[int, int, float]:
    """Token counts + cost from a provider usage dict. Chat completions report
    prompt/completion_tokens; the Responses API reports input/output_tokens.
    Bill either shape. Explicit key-presence checks (not `or`-chaining) so a
    legitimate 0 is never overridden."""
    if "prompt_tokens" in usage:
        prompt = int(usage.get("prompt_tokens") or 0)
    else:
        prompt = int(usage.get("input_tokens") or 0)
    if "completion_tokens" in usage:
        completion = int(usage.get("completion_tokens") or 0)
    else:
        completion = int(usage.get("output_tokens") or 0)
    cost = prompt * (model.input_cost_per_token or 0.0) + completion * (
        model.output_cost_per_token or 0.0
    )
    return prompt, completion, cost


def _reservation_cost(model: Model, request: dict[str, Any]) -> float:
    """Pessimistic pre-dispatch cost of a request: the estimated prompt plus
    the requested output ceiling per choice — `n` choices each regenerate the
    full output ceiling (providers bill the prompt once). A request without a
    max-tokens field (or on an unpriced model) reserves only what can be
    known — those bursts stay bounded by the prompt estimate alone. Callers
    pass the sanitized request, so `n`/max-tokens are already clamped."""
    prompt = _estimate_tokens(len(_request_text(request))) * (model.input_cost_per_token or 0.0)
    n = request.get("n")
    choices = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
    output = _max_output_tokens(request) * choices * (model.output_cost_per_token or 0.0)
    return prompt + output


class InFlightSpend:
    """Estimated cost of admitted-but-unsettled requests, per team.

    The budget gate adds this to committed spend, so a burst of concurrent
    requests (streams especially — they settle minutes after admission) can't
    all slip under a nearly-exhausted limit: each admission immediately
    reserves its pessimistic cost until settlement releases it. In-memory and
    per-process — replicas don't see each other's in-flight spend, so the
    overshoot bound is per replica, not global."""

    def __init__(self) -> None:
        self._by_team: dict[UUID, float] = {}

    def total(self, team_id: UUID) -> float:
        return self._by_team.get(team_id, 0.0)

    def add(self, team_id: UUID, amount: float) -> None:
        if amount > 0:
            self._by_team[team_id] = self._by_team.get(team_id, 0.0) + amount

    def remove(self, team_id: UUID, amount: float) -> None:
        if amount <= 0:
            return
        remaining = self._by_team.get(team_id, 0.0) - amount
        if remaining <= 0:
            self._by_team.pop(team_id, None)
        else:
            self._by_team[team_id] = remaining


class UsageMeter:
    """Admission, settlement, and tracing for one request's spend."""

    def __init__(
        self,
        usage: UsageRepository,
        emit_trace: Callable[[TraceRecord], None],
        budgets: BudgetRepository | None = None,
        in_flight: InFlightSpend | None = None,
        settlement_timeout: float = 30.0,
    ) -> None:
        self._usage = usage
        self._emit_trace = emit_trace
        self._budgets = budgets
        # Library use may omit it; the web wiring passes one shared instance so
        # request-scoped meters see each other's reservations.
        self._in_flight = in_flight if in_flight is not None else InFlightSpend()
        # Upper bound on the shielded stream settlement, so a stalled DB can't
        # leave an unbounded pile of orphan cleanup coroutines (M29).
        self._settlement_timeout = settlement_timeout

    async def admit(self, team_id: UUID, model: Model, request: dict[str, Any]) -> float:
        """Pre-call spend gate: reject once committed spend plus the estimated
        cost already reserved by in-flight requests reaches the budget limit.
        An admitted request immediately reserves its own pessimistic cost
        (prompt estimate + requested output ceiling) and returns it — callers
        release it at settlement. This bounds burst overshoot per replica:
        without the reservation, any number of concurrent requests could pass
        the gate before the first one settles (streams widen that blind spot
        to minutes)."""
        if self._budgets is None:
            return 0.0
        budget = await self._budgets.get(team_id)
        if budget is None:
            return 0.0
        since = window_start(budget.window, datetime.now(UTC))
        spent = await self._usage.spend_since(team_id, since)
        # No await between reading the in-flight total and adding the new
        # reservation: concurrent gates interleave only at checkpoints, so
        # two requests can't both read the same total and slip through.
        reserved = self._in_flight.total(team_id)
        if spent + reserved >= budget.limit_cost:
            raise BudgetExceeded(
                f"Team budget exceeded: spent {spent:.4f} (+{reserved:.4f} USD reserved "
                f"by in-flight requests) of {budget.limit_cost:.4f} USD "
                f"in the current {budget.window} window"
            )
        reservation = _reservation_cost(model, request)
        self._in_flight.add(team_id, reservation)
        return reservation

    def release(self, team_id: UUID, reservation: float) -> None:
        """Give back a reservation taken at admission (settlement or failure)."""
        self._in_flight.remove(team_id, reservation)

    async def settle_ok(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        response: dict[str, Any],
        latency_ms: float,
        request: dict[str, Any] | None = None,
    ) -> None:
        """Record usage (billing) + emit an observability trace. Fail-safe.

        If the provider reported no usable token counts (e.g. an adapter that
        omits usage), estimate the prompt from the request rather than billing
        zero silently — the non-streaming mirror of the stream estimate (H14)."""
        usage = response.get("usage") or {}
        if not _has_tokens(usage) and request is not None:
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
        prompt, completion, cost = _parse_usage(model, usage)
        now = datetime.now(UTC)
        await self._bill(team_id, api_key_id, model, operation, prompt, completion, cost, now)
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
            )
        )

    def trace_error(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        latency_ms: float,
        exc: BaseException,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
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
        api_key_id: UUID,
        model: Model,
        operation: str,
        prompt: int,
        completion: int,
        cost: float,
        now: datetime,
    ) -> None:
        """Persist the authoritative billing record (no trace — callers emit
        their own 'ok' or 'error' trace alongside)."""
        await self._record_usage(
            UsageEvent(
                id=uuid4(),
                team_id=team_id,
                api_key_id=api_key_id,
                model_id=model.id,
                model_name=model.name,
                operation=operation,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost=cost,
                created_at=now,
            )
        )

    async def metered_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        release: Callable[[], None] | None = None,
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
        try:
            async for chunk in stream:
                # OpenAI chat puts usage at the top level (final chunk); the
                # Responses API nests it under `response`.
                found = chunk.get("usage") or (chunk.get("response") or {}).get("usage")
                if found:
                    usage = found
                streamed_chars += len(_chunk_output_text(chunk))
                yield chunk
        except Exception as exc:
            error = exc
            raise
        finally:
            # Synchronous and first, so the budget reservation is released even
            # when a client disconnect cancelled this scope (a cancelled frame
            # runs sync code fine; it's the next checkpoint that re-raises).
            # release() is idempotent — the caller also finalizes it for the
            # never-iterated case (M27), so a double call here is safe.
            if release is not None:
                release()
            # Shielded: on a client disconnect this frame is already cancelled,
            # and the settlement's first checkpoint (the DB commit) would
            # re-raise CancelledError — no ledger row, no outbox, no trace.
            with anyio.CancelScope(shield=True):
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
                )

    async def metered_native_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        release: Callable[[], None] | None = None,
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
            # Synchronous release first (survives a cancelled scope), then the
            # shielded settlement — same ordering and guarantees as metered_stream.
            if release is not None:
                release()
            with anyio.CancelScope(shield=True):
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
                )

    async def metered_gemini_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        release: Callable[[], None] | None = None,
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
            # Synchronous release first (survives a cancelled scope), then the
            # shielded settlement — same ordering and guarantees as metered_stream.
            if release is not None:
                release()
            with anyio.CancelScope(shield=True):
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
    ) -> None:
        """Post-stream settlement: estimate usage if none arrived, bill, and
        trace. Runs inside `metered_stream`'s shielded finally — callers must
        already hold the cancellation shield."""
        latency_ms = (perf_counter() - start) * 1000
        # A provider that rejects the request before emitting anything
        # (error, zero streamed output, no usage reported) consumed
        # nothing upstream — don't fabricate a prompt estimate to bill it
        # (M26). A client disconnect (no error) still estimates and bills:
        # there the provider did consume the prompt. A mid-stream failure
        # after some output also bills — those tokens were paid upstream.
        produced_nothing = error is not None and streamed_chars == 0 and not _has_tokens(usage)
        if not _has_tokens(usage) and not produced_nothing:
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
                prompt, completion, cost = _parse_usage(model, usage)
                if _has_tokens(usage):
                    await self._bill(
                        team_id,
                        api_key_id,
                        model,
                        operation,
                        prompt,
                        completion,
                        cost,
                        datetime.now(UTC),
                    )
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
                await self.settle_ok(
                    team_id, api_key_id, model, operation, {"usage": usage}, latency_ms
                )
        if settle_scope.cancelled_caught:
            logger.error(
                "stream settlement timed out after %ss; spend may be unrecorded: "
                "team=%s model=%s op=%s",
                self._settlement_timeout,
                team_id,
                model.name,
                operation,
            )
