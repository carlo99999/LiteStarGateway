"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import anyio

from litestar_gateway.domain.budget import window_start
from litestar_gateway.domain.entities import Model, ModelType, TraceRecord, UsageEvent
from litestar_gateway.domain.exceptions import (
    BudgetExceeded,
    CredentialNotFound,
    ModelDisabled,
    ModelNotFound,
    ModelTypeMismatch,
)
from litestar_gateway.domain.ports import (
    BudgetRepository,
    CredentialRepository,
    LLMGateway,
    ModelRepository,
    UsageRepository,
)
from litestar_gateway.domain.request_policy import sanitize_request

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
    """Output text carried by one stream chunk (chat delta or Responses event)."""
    if chunk.get("type") == "response.output_text.delta":
        delta = chunk.get("delta")
        return delta if isinstance(delta, str) else ""
    choices = chunk.get("choices") or []
    if choices and isinstance(choices[0], dict):
        content = (choices[0].get("delta") or {}).get("content")
        return content if isinstance(content, str) else ""
    return ""


def _has_tokens(usage: dict[str, Any]) -> bool:
    return any(
        int(usage.get(key) or 0)
        for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
    )


class CompletionService:
    def __init__(
        self,
        models: ModelRepository,
        credentials: CredentialRepository,
        gateway: LLMGateway,
        usage: UsageRepository,
        emit_trace: Callable[[TraceRecord], None],
        budgets: BudgetRepository | None = None,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._usage = usage
        self._emit_trace = emit_trace
        self._budgets = budgets

    async def _record_usage(self, event: UsageEvent) -> None:
        """Persist the billing record. A failed write must never fail the request,
        but it must not vanish either: on failure the event is dead-lettered to a
        durable outbox and retried by the background reconciler. Only if that also
        fails do we fall back to an ERROR log with the full event (no secrets)."""
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

    async def _observe(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        response: dict[str, Any],
        latency_ms: float,
    ) -> None:
        """Record usage (billing) + emit an observability trace. Fail-safe."""
        usage = response.get("usage") or {}
        # Chat completions report prompt/completion_tokens; the Responses API
        # reports input/output_tokens. Bill either shape. Explicit key-presence
        # checks (not `or`-chaining) so a legitimate 0 is never overridden.
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
        now = datetime.now(UTC)
        # Usage = authoritative billing record (persisted).
        event = UsageEvent(
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
        await self._record_usage(event)
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

    def _emit_error_trace(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        latency_ms: float,
        exc: BaseException,
    ) -> None:
        """Emit a status='error' trace for a failed gateway call. Without this,
        provider outages/timeouts/rate-limits are invisible in tracing — exactly
        the events operators most need to see. No UsageEvent: there is no usage
        to bill (the provider reported none)."""
        self._emit_trace(
            TraceRecord(
                team_id=team_id,
                api_key_id=api_key_id,
                model_name=model.name,
                provider=model.provider.value,
                operation=operation,
                prompt_tokens=0,
                completion_tokens=0,
                cost=0.0,
                latency_ms=latency_ms,
                status="error",
                created_at=datetime.now(UTC),
                error_type=type(exc).__name__,
            )
        )

    async def _dispatch(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        call: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Run one gateway call, observing success (usage + trace) and failure
        (error trace) before the exception propagates to the HTTP layer."""
        start = perf_counter()
        try:
            response = await call()
        except Exception as exc:
            self._emit_error_trace(
                team_id, api_key_id, model, operation, (perf_counter() - start) * 1000, exc
            )
            raise
        latency_ms = (perf_counter() - start) * 1000
        await self._observe(team_id, api_key_id, model, operation, response, latency_ms)
        return response

    async def _enforce_budget(self, team_id: UUID) -> None:
        """Pre-call spend gate: reject once the team's accumulated cost in the
        current window reaches its budget limit. Enforcement reads recorded
        usage, so requests already in flight when the limit is crossed still
        complete (bounded overshoot — same semantics as other gateways)."""
        if self._budgets is None:
            return
        budget = await self._budgets.get(team_id)
        if budget is None:
            return
        since = window_start(budget.window, datetime.now(UTC))
        spent = await self._usage.spend_since(team_id, since)
        if spent >= budget.limit_cost:
            raise BudgetExceeded(
                f"Team budget exceeded: spent {spent:.4f} of {budget.limit_cost:.4f} USD "
                f"in the current {budget.window} window"
            )

    async def _prepare(
        self, team_id: UUID, request: dict[str, Any], expected_type: ModelType
    ) -> tuple[Model, dict[str, str]]:
        alias = request.get("model")
        model = await self._models.get_by_name(team_id, alias) if alias else None
        if model is None:
            raise ModelNotFound(str(alias))
        if not model.enabled:
            raise ModelDisabled(model.name)
        if model.type != expected_type:
            raise ModelTypeMismatch(
                f"Model '{model.name}' is type '{model.type}', not '{expected_type}'"
            )
        await self._enforce_budget(team_id)
        values = await self._credentials.get_values(model.credential_id)
        if values is None:
            raise CredentialNotFound(str(model.credential_id))
        return model, values

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("chat.completions", request)
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "chat.completions",
            lambda: self._gateway.achat_completion(clean, model, values),
        )

    async def responses(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("responses", request)
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "responses",
            lambda: self._gateway.aresponses(clean, model, values),
        )

    async def _metered_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Relay chunks unchanged while capturing usage as it flows, then record a
        UsageEvent + emit a trace once the stream finishes (or the client
        disconnects — the `finally` runs on generator close). Without this,
        streamed calls were neither billed nor observed. A provider error
        mid-stream emits a status='error' trace instead of a fake 'ok' one
        (a client disconnect still records as 'ok': bill what was seen).
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
            # Shielded: on a client disconnect this frame is already cancelled,
            # and the settlement's first checkpoint (the DB commit) would
            # re-raise CancelledError — no ledger row, no outbox, no trace.
            with anyio.CancelScope(shield=True):
                latency_ms = (perf_counter() - start) * 1000
                if error is not None:
                    self._emit_error_trace(team_id, api_key_id, model, operation, latency_ms, error)
                else:
                    # Even with zero streamed output (disconnect before the first
                    # content chunk) the provider consumed the prompt — bill it.
                    if not _has_tokens(usage):
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
                    await self._observe(
                        team_id, api_key_id, model, operation, {"usage": usage}, latency_ms
                    )

    async def open_chat_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts, metered for usage. Awaited before
        streaming starts so resolution errors surface as HTTP status codes."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("chat.completions", request)
        stream = await self._gateway.astream_chat_completion(clean, model, values)
        return self._metered_stream(team_id, api_key_id, model, "chat.completions", stream, clean)

    async def open_responses_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve (may raise → HTTP error) and return an async iterator of
        Responses-API stream events, metered for usage."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("responses", request)
        stream = await self._gateway.astream_responses(clean, model, values)
        return self._metered_stream(team_id, api_key_id, model, "responses", stream, clean)

    async def embeddings(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.EMBEDDINGS)
        clean = sanitize_request("embeddings", request)
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "embeddings",
            lambda: self._gateway.aembeddings(clean, model, values),
        )

    async def images(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.IMAGE)
        clean = sanitize_request("images", request)
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "images",
            lambda: self._gateway.aimages(clean, model, values),
        )
