"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials. Everything money-side —
budget admission, usage metering, billing, traces — is delegated to `UsageMeter`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.callable_aliases import CallableAliasResolver, ResolvedCallable
from litestar_gateway.application.guardrails.payloads import (
    can_redact_request,
    can_redact_response,
    redact_request,
    redact_response,
    request_text,
    response_text,
)
from litestar_gateway.application.guardrails.service import ChainedProvider, run_chain
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.chat_tool_policy import validate_chat_request
from litestar_gateway.domain.entities import Model, ModelType, Provider, UsageAttribution
from litestar_gateway.domain.exceptions import (
    CredentialNotFound,
    DomainError,
    GuardrailBlocked,
    ModelDisabled,
    ModelNotFound,
    ModelTypeMismatch,
    ProviderMismatch,
    UnsupportedOperation,
    UpstreamResponseInvalid,
    UpstreamTimeout,
)
from litestar_gateway.domain.failover import is_failover_eligible
from litestar_gateway.domain.guardrails import Direction, GuardrailPayload
from litestar_gateway.domain.ports import (
    Admission,
    CachedResponse,
    CacheKey,
    CircuitBreaker,
    CredentialRepository,
    LLMGateway,
    ModelRepository,
    ResponseCache,
    SemanticResponseCache,
)
from litestar_gateway.domain.request_policy import (
    clamp_native_output_tokens,
    clamp_output_tokens,
    native_reservation_view,
    reject_native_control_kwargs,
    sanitize_request,
    validate_responses_request,
)
from litestar_gateway.domain.response_cache_key import derive_cache_key, is_cacheable
from litestar_gateway.domain.response_cache_semantic import (
    derive_semantic_scope,
    extract_semantic_text,
    is_semantic_cacheable,
)
from litestar_gateway.domain.routing import (
    CandidateModel,
    RouterConfig,
    RoutingDecision,
    build_routing_context,
    filter_candidates,
)

# Resolves the chain for one (team, model, direction). A function rather than a
# repository so the wiring can cache, and so tests can hand over a literal chain.
# Resolves the chain for one (team, key, model, direction). The key is here
# because a judge guardrail makes a real, billable provider call: it has to be
# attributed to the key that caused it, or the safety layer looks free.
GuardrailChainFn = Callable[
    [UUID, UUID | None, Model, Direction, UUID | None], Awaitable[tuple[ChainedProvider, ...]]
]

logger = logging.getLogger("litestar_gateway.response_cache")

RequestValidator = Callable[[Model, dict[str, Any]], dict[str, Any]]


def _cache_usage_tokens(usage: dict[str, Any]) -> tuple[int, int] | None:
    """Token counts from a provider's authoritative usage block, for a
    response-cache write. `None` when the provider reported no usable counts —
    the response still settles and returns normally, it just isn't cached (the
    `UsageMeter` estimate-when-missing fallback, H14, is not replicated on this
    best-effort accelerator path)."""
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
    else:
        prompt = usage.get("input_tokens")
        completion = usage.get("output_tokens")
    if (
        isinstance(prompt, int)
        and not isinstance(prompt, bool)
        and isinstance(completion, int)
        and not isinstance(completion, bool)
    ):
        return prompt, completion
    return None


def _reject_unsupported_n(operation: str, model: Model, request: dict[str, Any]) -> None:
    """Reject a chat request asking for more than one completion (`n>1`) on a
    provider whose translator ignores `n`. Anthropic/Vertex/Bedrock always
    return exactly one completion, so honoring the request would silently
    under-deliver while the budget reservation charged the output ceiling
    per requested choice (up to MAX_N×), spuriously tripping BudgetExceeded
    for teams nowhere near their cap. Rejecting keeps the reservation and the
    provider's actual behavior in agreement (R7-M50). `n` lives only on the
    chat allowlist; other operations pass through untouched."""
    if operation != "chat.completions" or model.provider.honors_n:
        return
    n = request.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' does not support multiple completions "
            f"(n={n}); it returns exactly one completion per request"
        )


def _gemini_usage(response: dict[str, Any]) -> dict[str, Any]:
    """A usage-only view of a raw Gemini `GenerateContentResponse`, mapped to the
    shape `settle_ok`/`_parse_usage` read. Gemini reports usage under the native
    `usageMetadata` block (`promptTokenCount`/`candidatesTokenCount`); map it to
    `input_tokens`/`output_tokens` — the inverse of `from_gemini_response`'s usage
    extraction — so the native token counts are billed without translating (or
    even returning) the response body itself."""
    meta = response.get("usageMetadata") or {}
    return {
        "usage": {
            "input_tokens": meta.get("promptTokenCount"),
            "output_tokens": meta.get("candidatesTokenCount"),
        }
    }


async def _empty_stream() -> AsyncIterator[dict[str, Any]]:
    """An async iterator that yields nothing (empty provider stream)."""
    return
    yield  # unreachable — makes this a generator, not a plain coroutine


async def _rechain(
    first: dict[str, Any], rest: AsyncIterator[dict[str, Any]]
) -> AsyncIterator[dict[str, Any]]:
    """Re-emit an already-pulled first chunk, then delegate to the rest of the
    stream. The `finally` closes `rest` so the metered generator's billing/
    release settlement still runs when a client disconnects (`aclose()` on this
    wrapper): a bare `async for` does not propagate the close to `rest`."""
    try:
        yield first
        async for chunk in rest:
            yield chunk
    finally:
        aclose = getattr(rest, "aclose", None)
        if aclose is not None:
            await aclose()


def _synthetic_chat_chunks(cached_body: dict[str, Any], model: Model) -> list[dict[str, Any]]:
    """Re-chunk a cached, non-streamed `chat.completions` body into the same
    `chat.completion.chunk` wire shape `open_chat_stream` emits (Plan 04 Phase
    1, design §5): a role-only opening delta, the message content split into
    fixed-size pieces (so a client sees genuine incremental deltas rather than
    the whole body in one chunk), any tool calls as one delta (synthetic
    replay does not need real per-argument-token granularity), a closing delta
    carrying the original `finish_reason`, and a trailing usage-only chunk
    mirroring the shape `astream_chat_completion` adapters emit today. Purely
    a wire-shape formatter — billing is settled separately via
    `settle_cache_hit` with the *stored* authoritative counts, never re-derived
    from this synthetic usage chunk."""
    choice = (cached_body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or "stop"
    base = {
        "id": cached_body.get("id", "chatcmpl-cache"),
        "object": "chat.completion.chunk",
        "created": cached_body.get("created", int(time.time())),
        "model": model.provider_model_id,
    }

    def _delta_chunk(delta: dict[str, Any], finish: str | None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    chunks: list[dict[str, Any]] = [_delta_chunk({"role": message.get("role", "assistant")}, None)]
    content = message.get("content")
    if isinstance(content, str) and content:
        chunk_size = 20
        for start in range(0, len(content), chunk_size):
            chunks.append(_delta_chunk({"content": content[start : start + chunk_size]}, None))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        chunks.append(_delta_chunk({"tool_calls": tool_calls}, None))
    chunks.append(_delta_chunk({}, finish_reason))
    usage = cached_body.get("usage") or {}
    chunks.append({**base, "choices": [], "usage": usage})
    return chunks


async def _prime(gen: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Pull the first chunk now so a provider error at stream *open* raises here
    — before the controller commits the SSE `200 OK` — instead of mid-response
    where it can only abort the connection (the behaviour the endpoint comment
    always claimed but only half-delivered, R7-H24). The metered generator's own
    finally still bills/releases on both the error and normal-completion paths,
    so priming changes only *when* the first provider round-trip happens, not the
    accounting."""
    try:
        first = await anext(gen)
    except StopAsyncIteration:
        return _empty_stream()
    return _rechain(first, gen)


class CompletionService:
    def __init__(
        self,
        models: ModelRepository,
        credentials: CredentialRepository,
        gateway: LLMGateway,
        meter: UsageMeter,
        router_service: RouterService | None = None,
        callable_resolver: CallableAliasResolver | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        response_cache: ResponseCache | None = None,
        response_cache_ttl_s: int = 3600,
        semantic_cache: SemanticResponseCache | None = None,
        semantic_threshold: float = 0.97,
        semantic_embedding_model: str | None = None,
        guardrails: GuardrailChainFn | None = None,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._meter = meter
        self._router_service = router_service
        self._callable_resolver = callable_resolver
        self._circuit_breaker = circuit_breaker
        # None (the global RESPONSE_CACHE_ENABLED kill-switch off) makes every
        # cache access below inert: `_dispatch` never receives a `cache_key`
        # unless `self._response_cache` is set, so a disabled cache adds no
        # lookup, no write, byte-identical behavior (Plan 04 Phase 0).
        self._response_cache = response_cache
        self._response_cache_ttl_s = response_cache_ttl_s
        # Semantic tier (Plan 04 Phase 2). `semantic_cache=None` (the global
        # kill-switch off, same as `response_cache`) makes it inert exactly
        # like the exact-match tier; `semantic_embedding_model=None` (no
        # embedding model name configured platform-wide) makes it inert too,
        # even for a model that opted in — a missing embedder is treated as
        # semantic-ineligible, never an error (design §8).
        self._semantic_cache = semantic_cache
        self._semantic_threshold = semantic_threshold
        self._semantic_embedding_model = semantic_embedding_model
        # Guardrail chains, resolved per (team, model, direction). `None` — and a
        # resolver that returns an empty chain — mean the request path is
        # byte-identical to having no guardrails at all, which is what keeps this
        # off by default for every existing tenant.
        self._guardrails = guardrails

    async def _candidate_model(self, team_id: UUID, candidate: CandidateModel) -> Model | None:
        if self._callable_resolver is not None:
            if candidate.model_id is not None:
                return await self._callable_resolver.resolve_model_id(team_id, candidate.model_id)
            resolved = await self._callable_resolver.resolve(team_id, candidate.model_name)
            if resolved is not None and resolved.kind is CallableKind.MODEL:
                assert isinstance(resolved.resource, Model)
                return resolved.resource
            return None
        return await self._models.get_by_name(team_id, candidate.model_name)

    async def _validated_router(
        self,
        router: RouterConfig,
        team_id: UUID,
        operation: str,
        request: dict[str, Any],
        request_validator: RequestValidator | None,
    ) -> RouterConfig:
        """Remove candidates that cannot honor the request before routing side effects."""
        if request_validator is None:
            return router
        accepted: list[CandidateModel] = []
        rejected: list[UnsupportedOperation] = []
        for candidate in router.candidates:
            model = await self._candidate_model(team_id, candidate)
            if model is None:
                raise ModelNotFound(candidate.model_name)
            candidate_request = clamp_output_tokens(operation, request, model.max_output_tokens)
            try:
                request_validator(model, candidate_request)
            except UnsupportedOperation as exc:
                rejected.append(exc)
            else:
                accepted.append(candidate)
        if not accepted:
            if rejected:
                raise rejected[0]
            return router
        if len(accepted) == len(router.candidates):
            return router
        accepted_default = next(
            (candidate for candidate in accepted if candidate.model_name == router.default_model),
            accepted[0],
        )
        return replace(
            router,
            candidates=tuple(accepted),
            default_model=accepted_default.model_name,
            default_model_id=accepted_default.model_id,
        )

    async def _dispatch(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        request: dict[str, Any],
        call: Callable[[], Awaitable[dict[str, Any]]],
        reservation: Admission | None = None,
        settle_view: Callable[[dict[str, Any]], dict[str, Any]] = lambda response: response,
        attribution: UsageAttribution | None = None,
        cache_key: CacheKey | None = None,
        semantic_text: str | None = None,
        router_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Run one gateway call, observing success (usage + trace) and failure
        (error trace) before the exception propagates to the HTTP layer. The
        budget reservation taken at admission is released either way. The request
        is passed to settlement so usage can be estimated if the provider
        reported none (H14). `settle_view` maps the raw response to the usage-only
        shape settlement reads (identity for OpenAI-shaped responses; the native
        Gemini path passes `_gemini_usage`), so the raw body is still returned to
        the caller verbatim while billing sees the native token counts.

        `cache_key` is the single response-cache integration point (Plan 04
        Phase 0, design §9): non-`None` only for non-streamed chat.completions/
        responses that opted in (`chat_completion`/`responses` compute it).
        A hit skips `call()` entirely, settles at $0 via `settle_cache_hit`, and
        returns the stored body; a miss falls through to the normal dispatch and
        writes the fresh response afterward. Any cache exception is caught by
        the helpers below and treated as a miss/no-op (design §8) — the cache
        is never a dependency of the money path.

        `semantic_text` is the Phase 2 semantic-tier participation decision
        (`_semantic_text_for`): non-`None` only when `cache_key` is also
        non-`None` — the semantic tier is never tried without the exact-match
        tier in front of it — and is only ever consulted on an exact-match
        miss, never in place of it."""
        start = perf_counter()
        if cache_key is not None:
            cached = await self._cache_get(cache_key)
            if cached is None and semantic_text is not None:
                cached = await self._semantic_get(
                    team_id, api_key_id, model, operation, request, semantic_text
                )
            if cached is not None:
                latency_ms = (perf_counter() - start) * 1000
                await self._meter.settle_cache_hit(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    cached.prompt_tokens,
                    cached.completion_tokens,
                    latency_ms,
                    attribution,
                )
                await self._meter.release(reservation)
                # Screened again on the way out. Only screened bodies are stored
                # (see below), so this is normally a no-op — but a rule added
                # after the entry was written has to apply to it too, or the
                # cache would answer with content the current policy refuses.
                return await self._guard_response(
                    team_id, api_key_id, model, operation, cached.body, router_id
                )
        try:
            try:
                response = await call()
            except UpstreamResponseInvalid as exc:
                latency_ms = (perf_counter() - start) * 1000
                await self._meter.settle_error(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    exc.billable_response,
                    latency_ms,
                    exc,
                    request,
                    attribution,
                )
                await self._attach_routing_usage(exc.billable_response)
                raise
            except Exception as exc:
                self._meter.trace_error(
                    team_id, api_key_id, model, operation, (perf_counter() - start) * 1000, exc
                )
                raise
            latency_ms = (perf_counter() - start) * 1000
            view = settle_view(response)
            await self._meter.settle_ok(
                team_id,
                api_key_id,
                model,
                operation,
                view,
                latency_ms,
                request,
                attribution,
            )
            await self._attach_routing_usage(response)
            # Response-side guardrails run AFTER settlement, deliberately. The
            # provider call already happened and its tokens were really
            # consumed; refusing to bill a blocked answer would hand anyone who
            # can trip the response guardrail a free channel. So: bill, then
            # refuse to hand the content back.
            guarded = await self._guard_response(
                team_id, api_key_id, model, operation, response, router_id
            )
            # Cache what the chain produced, not what the provider said. Storing
            # first meant the caller was refused while the raw answer stayed in
            # the cache, and the next identical request was served that answer
            # as a 200 without the chain running (ISSUE-036). A block raises
            # above this line, so refused content is never written at all.
            if cache_key is not None:
                await self._cache_put(cache_key, guarded, view)
                if semantic_text is not None:
                    await self._semantic_put(
                        team_id,
                        api_key_id,
                        model,
                        operation,
                        request,
                        semantic_text,
                        guarded,
                        view,
                    )
            return guarded
        finally:
            await self._meter.release(reservation)

    async def _guard_request(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        request: dict[str, Any],
        router_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Run the request-side chain, returning the request to actually send.

        Blocking raises out of here — before admission, so nothing is reserved
        or billed for a call that never happens."""
        if self._guardrails is None:
            return request
        chain = await self._guardrails(team_id, api_key_id, model, Direction.REQUEST, router_id)
        if not chain:
            return request
        with self._traced_block(team_id, api_key_id, model, operation):
            outcome = await run_chain(
                chain,
                GuardrailPayload(
                    direction=Direction.REQUEST, text=request_text(request), raw=request
                ),
            )
            if not outcome.redacted:
                return request
            if not can_redact_request(request):
                # A redaction we cannot apply exactly is a redaction we do not
                # apply: guessing which multimodal block each piece of the
                # flattened text came from would be worse than refusing, and
                # passing the original through would defeat the point of the
                # verdict.
                raise GuardrailBlocked(
                    "content had to be redacted but the request shape cannot be rewritten safely"
                )
            return redact_request(request, outcome.text)

    async def _guard_response(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        response: dict[str, Any],
        router_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Run the response-side chain on an already-billed response."""
        if self._guardrails is None:
            return response
        chain = await self._guardrails(team_id, api_key_id, model, Direction.RESPONSE, router_id)
        if not chain:
            return response
        # The `ok` trace for the billed call was already emitted; this adds a
        # second, `error` trace for the refusal. Two rows for one request is the
        # honest record: the provider really was called and billed, AND the
        # caller really was refused.
        with self._traced_block(team_id, api_key_id, model, operation):
            outcome = await run_chain(
                chain,
                GuardrailPayload(
                    direction=Direction.RESPONSE, text=response_text(response), raw=response
                ),
            )
            if not outcome.redacted:
                return response
            if not can_redact_response(response):
                raise GuardrailBlocked(
                    "content had to be redacted but the response shape cannot be rewritten safely"
                )
            return redact_response(response, outcome.text)

    @contextmanager
    def _traced_block(
        self, team_id: UUID, api_key_id: UUID | None, model: Model, operation: str
    ) -> Iterator[None]:
        """Emit an `error` trace when the enclosed guardrail work refuses.

        Without this a refused request leaves no trace at all — the request hook
        runs before the dispatch that would have emitted one — so the console
        would show a team nothing but a drop in traffic. The trace carries the
        exception type and zero usage; it deliberately does not carry the
        message, because a guardrail's message names what was found and the
        trace table is not where that belongs.
        """
        start = perf_counter()
        try:
            yield
        except GuardrailBlocked as exc:
            self._meter.trace_error(
                team_id, api_key_id, model, operation, (perf_counter() - start) * 1000, exc
            )
            raise

    async def _cache_get(self, key: CacheKey) -> CachedResponse | None:
        """A cache failure must never fail the request (design §8): any
        exception from `get` is logged and treated as a miss."""
        assert self._response_cache is not None
        try:
            return await self._response_cache.get(key)
        except Exception:
            logger.warning("response cache get failed; treating as a miss", exc_info=True)
            return None

    async def _cache_put(
        self, key: CacheKey, response: dict[str, Any], usage_view: dict[str, Any]
    ) -> None:
        """Store a fresh, successful response for `key`. Only successful,
        fully-formed responses ever reach here (the write sits after
        `settle_ok`, design §7); a response whose usage can't be read cleanly
        is simply not cached. Any store exception is logged and swallowed."""
        assert self._response_cache is not None
        tokens = _cache_usage_tokens(usage_view.get("usage") or {})
        if tokens is None:
            return
        prompt, completion = tokens
        try:
            await self._response_cache.put(
                key,
                CachedResponse(body=response, prompt_tokens=prompt, completion_tokens=completion),
                self._response_cache_ttl_s,
            )
        except Exception:
            logger.warning("response cache put failed; continuing uncached", exc_info=True)

    async def _embed_for_semantic_cache(self, team_id: UUID, text: str) -> list[float] | None:
        """Best-effort embed of `text` via the platform's configured semantic-
        cache embedding model (`RESPONSE_CACHE_SEMANTIC_EMBEDDING_MODEL`,
        resolved per-team by name), reusing the gateway's own embeddings port
        exactly as the S3 embeddings routing strategy does
        (`application/routing/embeddings.py`). Returns `None` — never raises —
        when the model name isn't configured, the resolved model is missing/
        disabled/not an embeddings model, or its credential is missing; the
        caller's own try/except also treats a provider-call exception here as
        a miss (design §8)."""
        if self._semantic_embedding_model is None:
            return None
        embed_model = await self._models.get_by_name(team_id, self._semantic_embedding_model)
        if (
            embed_model is None
            or not embed_model.enabled
            or embed_model.type is not ModelType.EMBEDDINGS
        ):
            return None
        values = await self._credentials.get_values(embed_model.credential_id)
        if values is None:
            return None
        response = await self._gateway.aembeddings(
            {"model": embed_model.name, "input": [text]}, embed_model, values
        )
        data = response.get("data")
        if not isinstance(data, list) or not data:
            return None
        embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
        return embedding if isinstance(embedding, list) else None

    async def _semantic_get(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        request: dict[str, Any],
        text: str,
    ) -> CachedResponse | None:
        """Semantic-tier lookup (Plan 04 Phase 2, design §1/§8): embed `text`
        and search *only* this request's own scope — tenant, model, operation
        and the digest of every other behaviour-affecting field
        (`SemanticResponseCache.find`'s hard invariant, ISSUE-023), so
        similarity can only ever blur the text itself. Any exception —
        embedding failure or backend error — is logged and treated as a miss,
        exactly like `_cache_get`; the semantic tier is exactly as optional as
        the exact-match tier it sits behind."""
        assert self._semantic_cache is not None
        try:
            vector = await self._embed_for_semantic_cache(team_id, text)
            if vector is None:
                return None
            scope = derive_semantic_scope(
                team_id, api_key_id, model, operation, model.merge_params(request)
            )
            return await self._semantic_cache.find(scope, vector, self._semantic_threshold)
        except Exception:
            logger.warning("semantic cache lookup failed; treating as a miss", exc_info=True)
            return None

    async def _semantic_put(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        operation: str,
        request: dict[str, Any],
        text: str,
        response: dict[str, Any],
        usage_view: dict[str, Any],
    ) -> None:
        """Store a fresh, successful response in the semantic tier alongside
        its embedding, mirroring `_cache_put`'s cacheability/failure rules.
        Any exception is logged and swallowed — a semantic-write failure must
        never fail the request or fall back to failing the exact-match write
        (which already happened before this is called)."""
        assert self._semantic_cache is not None
        tokens = _cache_usage_tokens(usage_view.get("usage") or {})
        if tokens is None:
            return
        prompt, completion = tokens
        try:
            vector = await self._embed_for_semantic_cache(team_id, text)
            if vector is None:
                return
            scope = derive_semantic_scope(
                team_id, api_key_id, model, operation, model.merge_params(request)
            )
            await self._semantic_cache.add(
                scope,
                vector,
                CachedResponse(body=response, prompt_tokens=prompt, completion_tokens=completion),
                self._response_cache_ttl_s,
            )
        except Exception:
            logger.warning("semantic cache write failed; continuing without it", exc_info=True)

    async def _attach_routing_usage(self, response: dict[str, Any]) -> None:
        """Savings observability (§7): give the routing decision, if one was
        made for this request, its actual token usage. Non-streaming path;
        the streaming counterpart is `_metered`'s `on_settled` callback into
        `UsageMeter.metered_stream` (Plan 10 Phase 0) — both funnel into
        `_record_router_usage`, the single call to `RouterService.record_usage`."""
        usage = response.get("usage") or {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(prompt, int) and isinstance(completion, int):
            await self._record_router_usage(prompt, completion)

    async def _record_router_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Attach settled usage to this request's routing decision, if one was
        made (`RouterService.record_usage` is itself a no-op + fail-safe
        without a decision or on any attach failure). Shared by the
        non-streaming path (`_attach_routing_usage`) and the streaming
        settlement callback (`_metered`) so both contribute identically to
        the router's savings tracking."""
        if self._router_service is None:
            return
        await self._router_service.record_usage(prompt_tokens, completion_tokens)

    @staticmethod
    def _ensure_usable(model: Model | None, alias: object, expected_type: ModelType) -> Model:
        """The model must exist, be enabled, and match the operation's type.

        The three guards every resolution path shares (OpenAI-compatible and
        native alike), factored out so neither surface drifts from the other."""
        if model is None:
            raise ModelNotFound(str(alias))
        if not model.enabled:
            raise ModelDisabled(model.name)
        if model.type != expected_type:
            raise ModelTypeMismatch(
                f"Model '{model.name}' is type '{model.type}', not '{expected_type}'"
            )
        return model

    async def _resolve_model(self, team_id: UUID, alias: str | None) -> Model | None:
        if not alias:
            return None
        if self._callable_resolver is None:
            return await self._models.get_by_name(team_id, alias)
        resolved = await self._callable_resolver.resolve(team_id, alias)
        if resolved is None or resolved.kind is not CallableKind.MODEL:
            return None
        assert isinstance(resolved.resource, Model)
        return resolved.resource

    @staticmethod
    def _usage_attribution(
        team_id: UUID,
        alias: str | None,
        model: Model,
        resolved: ResolvedCallable | None = None,
    ) -> UsageAttribution:
        if resolved is not None:
            return UsageAttribution(
                requested_alias=alias,
                callable_origin=resolved.binding.origin.value,
                source_team_id=resolved.binding.source_team_id,
            )
        origin = (
            "global" if model.team_id is None else "own" if model.team_id == team_id else "extended"
        )
        return UsageAttribution(
            requested_alias=alias,
            callable_origin=origin,
            source_team_id=model.origin_team_id or model.team_id,
        )

    async def prepare_native(
        self, team_id: UUID, expected_type: ModelType, alias: str | None, data: dict[str, Any]
    ) -> tuple[Model, dict[str, str], dict[str, Any], UsageAttribution]:
        """Resolve a provider-native request's model `alias` to a usable team
        `Model` plus its decrypted credentials, and return the *governed* body.

        The alias is passed explicitly because the native protocols disagree on
        where it lives: Anthropic Messages carries it in the request body, Gemini
        carries it in the URL path. Runs the *same* enable/type/credential guards
        as `_prepare`, minus smart routing (native endpoints resolve one concrete
        same-protocol model). Budget admission stays with the caller (the native
        surface meters natively around its own dispatch), but the two governance
        guards the OpenAI surface applies are reapplied centrally here so every
        native method — streaming and non-streaming, both providers — gets them:
        reserved SDK control kwargs are rejected (credential-override vector,
        ISSUE-001) and the output-token field is clamped to the per-model/global
        ceiling (ISSUE-003). Everything else in the body stays verbatim. The
        upstream `base_url` still comes only from the credential (`get_values`),
        never from the client."""
        resolved = (
            await self._callable_resolver.resolve(team_id, alias)
            if alias and self._callable_resolver is not None
            else None
        )
        candidate: Model | None = None
        if resolved is not None and resolved.kind is CallableKind.MODEL:
            assert isinstance(resolved.resource, Model)
            candidate = resolved.resource
        elif alias and self._callable_resolver is None:
            candidate = await self._models.get_by_name(team_id, alias)
        model = self._ensure_usable(candidate, alias, expected_type)
        values = await self._credentials.get_values(model.credential_id)
        if values is None:
            raise CredentialNotFound(str(model.credential_id))
        reject_native_control_kwargs(data)
        governed = clamp_native_output_tokens(model.provider, data, model.max_output_tokens)
        return model, values, governed, self._usage_attribution(team_id, alias, model, resolved)

    async def native_messages(
        self, team_id: UUID, api_key_id: UUID, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Anthropic-native `/v1/messages` passthrough, metered around its own
        dispatch.

        Resolves + guards the model (`prepare_native`), rejects non-Anthropic
        models — `/v1/messages` is the Anthropic wire shape, so any other provider
        behind it is a misconfiguration, not a translation opportunity — then runs
        the money core: `admit` reserves the pessimistic cost from the native body
        (it carries Anthropic's required `max_tokens`), `_dispatch` calls the
        gateway's native method (no translation) and settles on the native `usage`
        block (`input_tokens`/`output_tokens`, which `_parse_usage` reads
        directly), releasing the reservation either way. Only the governance
        fields are touched (reserved-kwarg rejection + output-token clamp in
        `prepare_native`); the rest of the body flows to the provider verbatim."""
        model, values, governed, attribution = await self.prepare_native(
            team_id, ModelType.CHAT, data.get("model"), data
        )
        if model.provider is not Provider.ANTHROPIC:
            raise ProviderMismatch(
                f"Model '{model.name}' is provider '{model.provider.value}', not Anthropic; "
                "the native Messages endpoint (/v1/messages) serves Anthropic models only"
            )
        view = native_reservation_view(model.provider, governed)
        reservation = await self._meter.admit(team_id, model, view, api_key_id=api_key_id)
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "native.messages",
            view,
            lambda: self._gateway.anative_messages(governed, model, values),
            reservation,
            attribution=attribution,
        )

    async def open_native_messages_stream(
        self, team_id: UUID, api_key_id: UUID, data: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Anthropic-native `/v1/messages` streaming passthrough, metered natively.

        Mirrors `open_chat_stream` on top of `native_messages`' guards: resolve +
        guard the model (`prepare_native` → Anthropic-only via `ProviderMismatch`),
        `admit` the pessimistic cost from the native body (it carries Anthropic's
        required `max_tokens`), open the RAW Anthropic event stream (releasing the
        reservation on an open error), wrap it in the native metered generator, and
        prime the first event so an open-time provider error surfaces as an HTTP
        status BEFORE the SSE 200 commits (H24). The events flow through
        untranslated; usage is accumulated from the raw events and settled at the
        tail (or on disconnect — `_rechain`'s aclose propagation)."""
        model, values, governed, attribution = await self.prepare_native(
            team_id, ModelType.CHAT, data.get("model"), data
        )
        if model.provider is not Provider.ANTHROPIC:
            raise ProviderMismatch(
                f"Model '{model.name}' is provider '{model.provider.value}', not Anthropic; "
                "the native Messages endpoint (/v1/messages) serves Anthropic models only"
            )
        view = native_reservation_view(model.provider, governed)
        reservation = await self._meter.admit(team_id, model, view, api_key_id=api_key_id)
        try:
            stream = await self._gateway.astream_native_messages(governed, model, values)
        except BaseException:
            await self._meter.release(reservation)
            raise
        gen = self._metered_native(
            team_id, api_key_id, model, stream, view, reservation, attribution
        )
        return await _prime(gen)

    def _metered_native(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        reservation: Admission | None,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Native mirror of `_metered`: wrap the raw Anthropic stream in the native
        metered generator (usage accumulated from the raw events) and release the
        budget reservation exactly once — the generator's finally when iterated, a
        `weakref.finalize` for the never-iterated (drop-before-first-byte) case."""
        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await self._meter.release(reservation)

        def release_from_finalizer() -> None:
            # A garbage-collection callback cannot await. `release_soon`
            # schedules the release when a loop is running and otherwise leaves
            # it to the reservation's TTL.
            nonlocal released
            if not released:
                released = True
                self._meter.release_soon(reservation)

        gen = self._meter.metered_native_stream(
            team_id,
            api_key_id,
            model,
            "native.messages",
            stream,
            request,
            release,
            attribution,
        )
        weakref.finalize(gen, release_from_finalizer)
        return gen

    async def generate_content(
        self, team_id: UUID, api_key_id: UUID, model_alias: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Gemini-native `generateContent` passthrough, metered around its own
        dispatch.

        The Gemini protocol carries the model alias in the URL PATH (not the body),
        so `model_alias` is passed in explicitly. Resolves + guards the model
        (`prepare_native`), rejects non-Vertex models — the `generateContent`
        endpoint is the Gemini wire shape, so any other provider behind it is a
        misconfiguration — then reserves, dispatches the native call (no
        translation) and settles on the native `usageMetadata`
        (`promptTokenCount`/`candidatesTokenCount`, mapped to the
        `input_tokens`/`output_tokens` `_parse_usage` reads), releasing the
        reservation either way. Only the governance fields are touched
        (`prepare_native`); the rest of the body flows to the provider verbatim and
        the raw Gemini response is returned untranslated. Routed through `_dispatch`
        (with `settle_view=_gemini_usage`) rather than a hand-rolled copy, so the
        H14 estimate-when-usage-absent fallback fires here too (ISSUE-004): the
        OpenAI-shaped reservation view is passed as the settlement request, so a
        response missing `usageMetadata` is estimated instead of billed as $0."""
        model, values, governed, attribution = await self.prepare_native(
            team_id, ModelType.CHAT, model_alias, data
        )
        if model.provider is not Provider.VERTEX_AI:
            raise ProviderMismatch(
                f"Model '{model.name}' is provider '{model.provider.value}', not Vertex/Gemini; "
                "the native Gemini endpoint (generateContent) serves Vertex models only"
            )
        view = native_reservation_view(model.provider, governed)
        reservation = await self._meter.admit(team_id, model, view, api_key_id=api_key_id)
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "native.generate_content",
            view,
            lambda: self._gateway.agenerate_content(governed, model, values),
            reservation,
            settle_view=_gemini_usage,
            attribution=attribution,
        )

    async def open_generate_content_stream(
        self, team_id: UUID, api_key_id: UUID, model_alias: str, data: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Gemini-native `streamGenerateContent` passthrough, metered natively.

        Mirrors `open_native_messages_stream` on top of `generate_content`'s guards:
        resolve + guard the model (`prepare_native` → Vertex-only via
        `ProviderMismatch`), reserve, open the RAW Gemini chunk stream (releasing the
        reservation on an open error), wrap it in the native metered generator, and
        prime the first chunk so an open-time provider error surfaces as an HTTP
        status BEFORE the SSE 200 commits (H24). The chunks flow through
        untranslated; usage is accumulated from the raw `usageMetadata` and settled
        at the tail (or on disconnect — `_rechain`'s aclose propagation)."""
        model, values, governed, attribution = await self.prepare_native(
            team_id, ModelType.CHAT, model_alias, data
        )
        if model.provider is not Provider.VERTEX_AI:
            raise ProviderMismatch(
                f"Model '{model.name}' is provider '{model.provider.value}', not Vertex/Gemini; "
                "the native Gemini endpoint (streamGenerateContent) serves Vertex models only"
            )
        view = native_reservation_view(model.provider, governed)
        reservation = await self._meter.admit(team_id, model, view, api_key_id=api_key_id)
        try:
            stream = await self._gateway.astream_generate_content(governed, model, values)
        except BaseException:
            await self._meter.release(reservation)
            raise
        gen = self._metered_gemini(
            team_id, api_key_id, model, stream, view, reservation, attribution
        )
        return await _prime(gen)

    def _metered_gemini(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        reservation: Admission | None,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Native mirror of `_metered_native` for the Gemini wire shape: wrap the raw
        Gemini chunk stream in the native metered generator (usage accumulated from
        the raw `usageMetadata`) and release the budget reservation exactly once —
        the generator's finally when iterated, a `weakref.finalize` for the
        never-iterated (drop-before-first-byte) case."""
        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await self._meter.release(reservation)

        def release_from_finalizer() -> None:
            # A garbage-collection callback cannot await. `release_soon`
            # schedules the release when a loop is running and otherwise leaves
            # it to the reservation's TTL.
            nonlocal released
            if not released:
                released = True
                self._meter.release_soon(reservation)

        gen = self._meter.metered_gemini_stream(
            team_id,
            api_key_id,
            model,
            "native.generate_content",
            stream,
            request,
            release,
            attribution,
        )
        weakref.finalize(gen, release_from_finalizer)
        return gen

    async def _prepare(
        self,
        team_id: UUID,
        operation: str,
        request: dict[str, Any],
        expected_type: ModelType,
        api_key_id: UUID | None,
        request_validator: RequestValidator | None = None,
        *,
        router_context: dict[str, Any] | None = None,
    ) -> tuple[Model, dict[str, str], Admission | None, dict[str, Any], UsageAttribution]:
        # Gate the caller before router strategies: judge/embedding strategies
        # may make billable provider calls while resolving a virtual model.
        # The later admit handles team RPM + budget and omits the key so this
        # external request consumes exactly one key-RPM hit.
        await self._meter.enforce_key_rate_limit(api_key_id)
        alias = request.get("model")
        model = None
        # The router the caller named, when the alias was one. Known here
        # because routing happens just below, before the guardrail hook —
        # a router-scoped rule needs it to outrank the resolved model's.
        routed_router_id: UUID | None = None
        resolved = (
            await self._callable_resolver.resolve(team_id, alias)
            if alias and self._callable_resolver is not None
            else None
        )
        if resolved is not None and resolved.kind is CallableKind.MODEL:
            assert isinstance(resolved.resource, Model)
            model = resolved.resource
        elif (
            resolved is not None
            and resolved.kind is CallableKind.ROUTER
            and self._router_service is not None
            and expected_type is ModelType.CHAT
        ):
            router = resolved.resource
            assert isinstance(router, RouterConfig)
            if router.enabled:
                router = await self._validated_router(
                    router, team_id, operation, request, request_validator
                )
                decision = await self._router_service.route(
                    router, request, acting_team_id=team_id, api_key_id=api_key_id
                )
                routed_router_id = router.id
                if router_context is not None:
                    router_context["router"] = router
                    router_context["decision"] = decision
                assert self._callable_resolver is not None
                if decision.model_id is not None:
                    model = await self._callable_resolver.resolve_model_id(
                        team_id, decision.model_id
                    )
                else:  # compatibility for in-memory/legacy router definitions
                    chosen = await self._callable_resolver.resolve(team_id, decision.model_name)
                    if chosen is not None and chosen.kind is CallableKind.MODEL:
                        assert isinstance(chosen.resource, Model)
                        model = chosen.resource
        elif self._callable_resolver is None:
            model = await self._models.get_by_name(team_id, alias) if alias else None
        if (
            self._callable_resolver is None
            and model is None
            and alias
            and self._router_service is not None
            and expected_type is ModelType.CHAT
        ):
            # Smart routing: the alias may name a router (virtual model). The
            # strategy only rewrites the model name; the rest of the pipeline
            # (clamping, budget admission, metering) runs on the chosen model.
            router = await self._router_service.get_enabled_by_name(team_id, alias)
            if router is not None:
                router = await self._validated_router(
                    router, team_id, operation, request, request_validator
                )
                decision = await self._router_service.route(
                    router, request, acting_team_id=team_id, api_key_id=api_key_id
                )
                routed_router_id = router.id
                if router_context is not None:
                    router_context["router"] = router
                    router_context["decision"] = decision
                model = await self._models.get_by_name(team_id, decision.model_name)
        model = self._ensure_usable(model, alias, expected_type)
        _reject_unsupported_n(operation, model, request)
        # Per-model output ceiling: clamp/inject now that the model is known, and
        # reserve from the clamped request so admission and the provider call agree.
        clean = clamp_output_tokens(operation, request, model.max_output_tokens)
        if request_validator is not None:
            clean = request_validator(model, clean)
        # Guardrails run BEFORE admission: a blocked request must not consume
        # the team's budget reservation or its rate-limit slot, because it never
        # reaches a provider. A redaction rewrites `clean`, so everything after
        # this line — admission, the trace, the provider call — sees the redacted
        # prompt and never the original.
        clean = await self._guard_request(
            team_id, api_key_id, model, operation, clean, routed_router_id
        )
        reservation = await self._meter.admit(team_id, model, model.merge_params(clean))
        try:
            values = await self._credentials.get_values(model.credential_id)
            if values is None:
                raise CredentialNotFound(str(model.credential_id))
        except BaseException:
            await self._meter.release(reservation)
            raise
        attribution = self._usage_attribution(team_id, alias, model, resolved)
        return model, values, reservation, clean, attribution

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID | None, request: dict[str, Any]
    ) -> dict[str, Any]:
        sanitized = sanitize_request("chat.completions", request)
        router_context: dict[str, Any] = {}
        model, values, reservation, clean, attribution = await self._prepare(
            team_id,
            "chat.completions",
            sanitized,
            ModelType.CHAT,
            api_key_id,
            validate_chat_request,
            router_context=router_context,
        )
        router = router_context.get("router")
        decision = router_context.get("decision")
        if isinstance(router, RouterConfig) and router.failover_enabled and decision is not None:
            assert isinstance(decision, RoutingDecision)
            return await self._chat_completion_with_failover(
                team_id,
                api_key_id,
                sanitized,
                model,
                values,
                reservation,
                clean,
                attribution,
                router,
                decision,
            )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "chat.completions",
            clean,
            lambda: self._gateway.achat_completion(clean, model, values),
            reservation,
            attribution=attribution,
            router_id=router.id if isinstance(router, RouterConfig) else None,
            cache_key=self._cache_key_for(team_id, api_key_id, "chat.completions", clean, model),
            semantic_text=self._semantic_text_for("chat.completions", clean, model),
        )

    def _cache_key_for(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        operation: str,
        request: dict[str, Any],
        model: Model,
    ) -> CacheKey | None:
        """The `_dispatch` cache-participation decision (Plan 04 Phase 0):
        `None` when the global kill-switch is off, the team+model hasn't
        opted in, or the request is otherwise ineligible (design §7) — in
        every one of those cases `_dispatch` performs no lookup and no write.
        Not wired into the cross-provider failover retry path (Plan 05): each
        retry targets a different candidate model, and caching a failover
        response is deferred to a later slice rather than reasoning about
        per-attempt keys under this Phase 0's time budget.

        Both the eligibility check and the key are derived from the *effective*
        request — the same `merge_params` view admission and the adapter use, so
        admin defaults and enforced policy are part of the key and an enforced
        `temperature` cannot slip past the non-determinism gate (ISSUE-023)."""
        if self._response_cache is None:
            return None
        effective = model.merge_params(request)
        if not is_cacheable(operation, effective, model):
            return None
        return derive_cache_key(team_id, api_key_id, model, operation, effective)

    def _semantic_text_for(
        self, operation: str, request: dict[str, Any], model: Model
    ) -> str | None:
        """The `_dispatch` semantic-tier participation decision (Plan 04
        Phase 2): `None` when the semantic tier's own kill-switch/embedding
        model aren't configured, the model hasn't separately opted in
        (`is_semantic_cacheable` — which itself requires exact-match
        eligibility, so semantic is never tried without exact-match in front
        of it), or there is no extractable text to embed. In every one of
        those cases `_dispatch` never calls `embed` and never attempts the
        semantic tier."""
        if self._semantic_cache is None or self._semantic_embedding_model is None:
            return None
        if not is_semantic_cacheable(operation, request, model):
            return None
        return extract_semantic_text(request)

    async def _remaining_failover_candidates(
        self,
        team_id: UUID,
        router: RouterConfig,
        decision: RoutingDecision,
        request: dict[str, Any],
    ) -> list[Model]:
        """The ordered fallback chain: `filter_candidates`' survivors in
        declared order, excluding whichever candidate the strategy already
        chose as attempt #1."""
        ctx = build_routing_context(request, team_id=team_id)
        survivors = filter_candidates(ctx, router.candidates)
        models: list[Model] = []
        for candidate in survivors:
            if candidate.model_name == decision.model_name:
                continue
            candidate_model = await self._candidate_model(team_id, candidate)
            if (
                candidate_model is not None
                and candidate_model.enabled
                and candidate_model.type is ModelType.CHAT
            ):
                models.append(candidate_model)
        return models

    async def _breaker_filtered(
        self, candidates: list[Model]
    ) -> tuple[list[Model], dict[str, str]]:
        """Drop candidates the circuit breaker has short-circuited (tripped
        past its failure threshold, still cooling down) from the retry chain,
        and return the half-open trial tokens this filtering was granted.

        A candidate admitted *through* a half-open trial owes the breaker that
        trial's outcome, so the token has to travel with it: only the holder may
        close or re-open the breaker (ISSUE-033). A probed candidate the chain
        never reaches leaves its trial unresolved until the marker expires —
        the same as before tokens existed, now visible rather than implicit.

        A no-op when no breaker is configured."""
        if self._circuit_breaker is None:
            return candidates, {}
        allowed: list[Model] = []
        trial_tokens: dict[str, str] = {}
        for candidate in candidates:
            lease = await self._circuit_breaker.allow(str(candidate.id))
            if not lease.allowed:
                continue
            allowed.append(candidate)
            if lease.trial_token is not None:
                trial_tokens[str(candidate.id)] = lease.trial_token
        return allowed, trial_tokens

    @asynccontextmanager
    async def _within_deadline(self, router: RouterConfig, start: float) -> AsyncIterator[None]:
        """Bound one failover attempt by whatever is left of the router's
        `overall_deadline_ms` wall-clock budget (ISSUE-027).

        The deadline used to be checked only *between* attempts, which made it
        a gate on starting a retry rather than a budget for the chain: a single
        slow attempt could run to the SDK timeout and blow past it entirely.
        The point of the property is precisely to stop
        `slow-timeout x candidates` from outlasting the caller's patience, so
        the budget has to cut off the attempt in flight.

        Expiry surfaces as `UpstreamTimeout`, which is failover-eligible: the
        loop's next iteration re-checks the budget, finds nothing left, and
        raises it. Release, settlement and breaker bookkeeping are unchanged —
        the timeout arrives as an exception through the same paths a provider
        timeout already takes."""
        if router.overall_deadline_ms is None:
            yield
            return
        remaining = router.overall_deadline_ms / 1000 - (perf_counter() - start)
        if remaining <= 0:
            raise UpstreamTimeout("failover deadline exceeded before the attempt started")
        try:
            async with asyncio.timeout(remaining):
                yield
        except TimeoutError as exc:
            raise UpstreamTimeout("failover deadline exceeded during the attempt") from exc

    async def _chat_completion_with_failover(
        self,
        team_id: UUID,
        api_key_id: UUID | None,
        sanitized: dict[str, Any],
        model: Model,
        values: dict[str, str],
        reservation: Admission | None,
        clean: dict[str, Any],
        attribution: UsageAttribution,
        router: RouterConfig,
        decision: RoutingDecision,
    ) -> dict[str, Any]:
        """Retry the remaining candidate chain on a failover-eligible error.
        Attempt #1 reuses the model/values/reservation `_prepare` already
        admitted; later attempts admit fresh (skipping the team-RPM gate --
        one logical request is one team-RPM hit, taken on attempt #1) and
        re-clamp/re-validate the *original* request for that candidate's own
        output ceiling and provider contract."""
        start = perf_counter()
        remaining = await self._remaining_failover_candidates(team_id, router, decision, sanitized)
        remaining, trial_tokens = await self._breaker_filtered(remaining)
        max_attempts = min(router.max_attempts, 1 + len(remaining))
        attempt_model, attempt_values, attempt_reservation, attempt_clean = (
            model,
            values,
            reservation,
            clean,
        )
        last_exc: DomainError | None = None
        attempts_made = 0
        try:
            for attempt_index in range(max_attempts):
                attempts_made = attempt_index + 1
                if attempt_index > 0:
                    assert last_exc is not None  # set by the previous iteration's failure
                    if (
                        router.overall_deadline_ms is not None
                        and (perf_counter() - start) * 1000 >= router.overall_deadline_ms
                    ):
                        raise last_exc
                    attempt_model = remaining[attempt_index - 1]
                    attempt_clean = clamp_output_tokens(
                        "chat.completions", sanitized, attempt_model.max_output_tokens
                    )
                    attempt_clean = validate_chat_request(attempt_model, attempt_clean)
                    # Rebuilding from `sanitized` is deliberate: each candidate
                    # clamps the caller's original ceiling rather than inheriting
                    # the previous candidate's. But `sanitized` is the body from
                    # *before* the guardrails ran, so reusing it alone restored
                    # whatever `_prepare` had redacted and shipped it to a
                    # different provider (ISSUE-035). The chain therefore runs
                    # again here — which it owed this attempt anyway: this is
                    # another model, with possibly its own rules, and the router
                    # the caller named is still the scope that applies.
                    attempt_clean = await self._guard_request(
                        team_id,
                        api_key_id,
                        attempt_model,
                        "chat.completions",
                        attempt_clean,
                        router.id,
                    )
                    attempt_values = await self._credentials.get_values(attempt_model.credential_id)
                    if attempt_values is None:
                        raise CredentialNotFound(str(attempt_model.credential_id))
                    attempt_reservation = await self._meter.admit(
                        team_id,
                        attempt_model,
                        attempt_model.merge_params(attempt_clean),
                        skip_team_rate_limit=True,
                    )
                try:
                    async with self._within_deadline(router, start):
                        response = await self._dispatch(
                            team_id,
                            api_key_id,
                            attempt_model,
                            "chat.completions",
                            attempt_clean,
                            lambda m=attempt_model, v=attempt_values, c=attempt_clean: (
                                self._gateway.achat_completion(c, m, v)
                            ),
                            attempt_reservation,
                            attribution=attribution,
                            # The caller named this router, on the first attempt
                            # and on every retry: without it the response chain
                            # resolves as if there were no router, so a
                            # router-scoped rule silently stopped applying the
                            # moment failover kicked in (ISSUE-035).
                            router_id=router.id,
                        )
                except DomainError as exc:
                    # UpstreamResponseInvalid already billed a partial charge
                    # inside _dispatch (settle_error) before re-raising; retrying
                    # it would double-bill the team for one logical request, so
                    # it is terminal here even though the general eligibility
                    # classifier marks it eligible (it subclasses
                    # UpstreamUnavailable, whose *other* members never bill). A
                    # client 4xx (not failover-eligible) never trips the breaker --
                    # it is the client's fault, not the provider's.
                    eligible = is_failover_eligible(exc) and not isinstance(
                        exc, UpstreamResponseInvalid
                    )
                    if eligible and self._circuit_breaker is not None:
                        await self._circuit_breaker.record_failure(
                            str(attempt_model.id), trial_tokens.get(str(attempt_model.id))
                        )
                    if not eligible or attempt_index == max_attempts - 1:
                        raise
                    last_exc = exc
                    continue
                if self._circuit_breaker is not None:
                    await self._circuit_breaker.record_success(
                        str(attempt_model.id), trial_tokens.get(str(attempt_model.id))
                    )
                return response
            raise AssertionError("unreachable: the loop above always returns or raises")
        finally:
            if self._router_service is not None:
                await self._router_service.record_failover(attempts_made, attempts_made > 1)

    async def responses(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("responses", request)
        model, values, reservation, clean, attribution = await self._prepare(
            team_id,
            "responses",
            clean,
            ModelType.CHAT,
            api_key_id,
            validate_responses_request,
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "responses",
            clean,
            lambda: self._gateway.aresponses(clean, model, values),
            reservation,
            attribution=attribution,
            cache_key=self._cache_key_for(team_id, api_key_id, "responses", clean, model),
            semantic_text=self._semantic_text_for("responses", clean, model),
        )

    async def open_chat_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts, metered for usage. Awaited before
        streaming starts so resolution errors surface as HTTP status codes.

        Response-cache participation (Plan 04 Phase 1, design §5, §9): the
        same `_cache_key_for` decision `chat_completion` uses (non-`None` only
        when the global switch, team+model opt-in, and request shape all
        agree — `stream` is never part of the key). A hit skips the provider
        entirely and replays the stored body as a synthetic SSE stream,
        settling at $0 via `settle_cache_hit`; a miss falls through to the
        normal streaming dispatch below unchanged. Not checked on the
        failover branch, mirroring Phase 0's failover-retry exclusion (a
        cached response was written for one specific candidate model)."""
        sanitized = sanitize_request("chat.completions", request)
        router_context: dict[str, Any] = {}
        model, values, reservation, clean, attribution = await self._prepare(
            team_id,
            "chat.completions",
            sanitized,
            ModelType.CHAT,
            api_key_id,
            validate_chat_request,
            router_context=router_context,
        )
        router = router_context.get("router")
        decision = router_context.get("decision")
        if isinstance(router, RouterConfig) and router.failover_enabled and decision is not None:
            assert isinstance(decision, RoutingDecision)
            return await self._open_chat_stream_with_failover(
                team_id,
                api_key_id,
                sanitized,
                model,
                values,
                reservation,
                clean,
                attribution,
                router,
                decision,
            )
        cache_key = self._cache_key_for(team_id, api_key_id, "chat.completions", clean, model)
        if cache_key is not None:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                gen = self._metered_cache_hit_stream(
                    team_id, api_key_id, model, "chat.completions", cached, reservation, attribution
                )
                return await _prime(gen)
        try:
            stream = await self._gateway.astream_chat_completion(clean, model, values)
        except BaseException:
            await self._meter.release(reservation)
            raise
        gen = self._metered(
            team_id,
            api_key_id,
            model,
            "chat.completions",
            stream,
            clean,
            reservation,
            attribution,
        )
        return await _prime(gen)

    def _metered_cache_hit_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        cached: CachedResponse,
        reservation: Admission | None,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Synthetic-stream replay of a cache hit (Plan 04 Phase 1). Mirrors
        `_metered`'s release-once + settlement machinery: the reservation is
        released exactly once (the generator's finally when iterated, a
        `weakref.finalize` for the never-iterated drop-before-first-byte
        case), and the tail settles through the *same* `settle_cache_hit`
        path `_dispatch`'s non-streamed hit uses — one billing path for both,
        never a second one for the streaming replay (design §6)."""
        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await self._meter.release(reservation)

        def release_from_finalizer() -> None:
            # A garbage-collection callback cannot await. `release_soon`
            # schedules the release when a loop is running and otherwise leaves
            # it to the reservation's TTL.
            nonlocal released
            if not released:
                released = True
                self._meter.release_soon(reservation)

        async def gen() -> AsyncIterator[dict[str, Any]]:
            start = perf_counter()
            try:
                for chunk in _synthetic_chat_chunks(cached.body, model):
                    yield chunk
            finally:
                await release()
                latency_ms = (perf_counter() - start) * 1000
                await self._meter.settle_cache_hit(
                    team_id,
                    api_key_id,
                    model,
                    operation,
                    cached.prompt_tokens,
                    cached.completion_tokens,
                    latency_ms,
                    attribution,
                )

        generator = gen()
        weakref.finalize(generator, release_from_finalizer)
        return generator

    async def _open_chat_stream_with_failover(
        self,
        team_id: UUID,
        api_key_id: UUID,
        sanitized: dict[str, Any],
        model: Model,
        values: dict[str, str],
        reservation: Admission | None,
        clean: dict[str, Any],
        attribution: UsageAttribution,
        router: RouterConfig,
        decision: RoutingDecision,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming failover, pre-first-byte only: a failover-eligible error at
        stream *open* or the first `anext` (before any chunk ever reaches this
        method's caller) retries the next candidate; once a chunk has been
        yielded, `_prime`'s job is already done and this method has already
        returned, so no later error can reach this retry loop at all. Unlike
        the non-streaming loop, no `UpstreamResponseInvalid` special case is
        needed here: `metered_stream`'s existing zero-consumption invariant
        (M26) already skips billing entirely whenever an error is raised with
        no chunk ever seen, which is unconditionally true for every failure
        this loop retries."""
        start = perf_counter()
        remaining = await self._remaining_failover_candidates(team_id, router, decision, sanitized)
        remaining, trial_tokens = await self._breaker_filtered(remaining)
        max_attempts = min(router.max_attempts, 1 + len(remaining))
        attempt_model, attempt_values, attempt_reservation, attempt_clean = (
            model,
            values,
            reservation,
            clean,
        )
        last_exc: DomainError | None = None
        attempts_made = 0
        try:
            for attempt_index in range(max_attempts):
                attempts_made = attempt_index + 1
                if attempt_index > 0:
                    assert last_exc is not None  # set by the previous iteration's failure
                    if (
                        router.overall_deadline_ms is not None
                        and (perf_counter() - start) * 1000 >= router.overall_deadline_ms
                    ):
                        raise last_exc
                    attempt_model = remaining[attempt_index - 1]
                    attempt_clean = clamp_output_tokens(
                        "chat.completions", sanitized, attempt_model.max_output_tokens
                    )
                    attempt_clean = validate_chat_request(attempt_model, attempt_clean)
                    # Rebuilding from `sanitized` is deliberate: each candidate
                    # clamps the caller's original ceiling rather than inheriting
                    # the previous candidate's. But `sanitized` is the body from
                    # *before* the guardrails ran, so reusing it alone restored
                    # whatever `_prepare` had redacted and shipped it to a
                    # different provider (ISSUE-035). The chain therefore runs
                    # again here — which it owed this attempt anyway: this is
                    # another model, with possibly its own rules, and the router
                    # the caller named is still the scope that applies.
                    attempt_clean = await self._guard_request(
                        team_id,
                        api_key_id,
                        attempt_model,
                        "chat.completions",
                        attempt_clean,
                        router.id,
                    )
                    attempt_values = await self._credentials.get_values(attempt_model.credential_id)
                    if attempt_values is None:
                        raise CredentialNotFound(str(attempt_model.credential_id))
                    attempt_reservation = await self._meter.admit(
                        team_id,
                        attempt_model,
                        attempt_model.merge_params(attempt_clean),
                        skip_team_rate_limit=True,
                    )
                try:
                    async with self._within_deadline(router, start):
                        stream = await self._gateway.astream_chat_completion(
                            attempt_clean, attempt_model, attempt_values
                        )
                except DomainError as exc:
                    # Never entered _metered, so nothing else releases this
                    # attempt's reservation -- we must release it ourselves.
                    await self._meter.release(attempt_reservation)
                    eligible = is_failover_eligible(exc)
                    if eligible and self._circuit_breaker is not None:
                        await self._circuit_breaker.record_failure(
                            str(attempt_model.id), trial_tokens.get(str(attempt_model.id))
                        )
                    if not eligible or attempt_index == max_attempts - 1:
                        raise
                    last_exc = exc
                    continue
                except BaseException:
                    await self._meter.release(attempt_reservation)
                    raise
                gen = self._metered(
                    team_id,
                    api_key_id,
                    attempt_model,
                    "chat.completions",
                    stream,
                    attempt_clean,
                    attempt_reservation,
                    attribution,
                )
                try:
                    async with self._within_deadline(router, start):
                        primed = await _prime(gen)
                except DomainError as exc:
                    # metered_stream's own shielded finally already released this
                    # reservation (via the release() closure _metered wired in)
                    # and already settled billing (nothing, per M26, since zero
                    # chunks were ever produced) -- do not release again here.
                    eligible = is_failover_eligible(exc)
                    if eligible and self._circuit_breaker is not None:
                        await self._circuit_breaker.record_failure(
                            str(attempt_model.id), trial_tokens.get(str(attempt_model.id))
                        )
                    if not eligible or attempt_index == max_attempts - 1:
                        raise
                    last_exc = exc
                    continue
                if self._circuit_breaker is not None:
                    await self._circuit_breaker.record_success(
                        str(attempt_model.id), trial_tokens.get(str(attempt_model.id))
                    )
                return primed
            raise AssertionError("unreachable: the loop above always returns or raises")
        finally:
            if self._router_service is not None:
                await self._router_service.record_failover(attempts_made, attempts_made > 1)

    async def open_responses_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve (may raise → HTTP error) and return an async iterator of
        Responses-API stream events, metered for usage."""
        clean = sanitize_request("responses", request)
        model, values, reservation, clean, attribution = await self._prepare(
            team_id,
            "responses",
            clean,
            ModelType.CHAT,
            api_key_id,
            validate_responses_request,
        )
        try:
            stream = await self._gateway.astream_responses(clean, model, values)
        except BaseException:
            await self._meter.release(reservation)
            raise
        gen = self._metered(
            team_id, api_key_id, model, "responses", stream, clean, reservation, attribution
        )
        return await _prime(gen)

    def _metered(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
        request: dict[str, Any],
        reservation: Admission | None,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Wrap the provider stream with usage metering, releasing the budget
        reservation exactly once. The metered generator releases it in its
        finally when iterated; a `weakref.finalize` covers the case where the
        SSE layer returns without ever starting it (client drops before the
        first byte) — otherwise the reservation would leak into InFlightSpend
        forever and eventually 402 the whole team (M27).

        `on_settled` (Plan 10 Phase 0) attaches the stream's settled usage to
        this request's routing decision the moment it is billed — the
        streaming counterpart of `_attach_routing_usage`, which only ever ran
        for non-streamed responses. `_record_router_usage` is itself a no-op
        without a router-routed request, and `metered_stream` swallows any
        exception the callback raises, so this can never break the stream or
        the billing that already committed."""
        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await self._meter.release(reservation)

        def release_from_finalizer() -> None:
            # A garbage-collection callback cannot await. `release_soon`
            # schedules the release when a loop is running and otherwise leaves
            # it to the reservation's TTL.
            nonlocal released
            if not released:
                released = True
                self._meter.release_soon(reservation)

        gen = self._meter.metered_stream(
            team_id,
            api_key_id,
            model,
            operation,
            stream,
            request,
            release,
            attribution,
            on_settled=self._record_router_usage,
        )
        weakref.finalize(gen, release_from_finalizer)
        return gen

    async def embeddings(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("embeddings", request)
        model, values, reservation, clean, attribution = await self._prepare(
            team_id, "embeddings", clean, ModelType.EMBEDDINGS, api_key_id
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "embeddings",
            clean,
            lambda: self._gateway.aembeddings(clean, model, values),
            reservation,
            attribution=attribution,
        )

    async def images(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("images", request)
        model, values, reservation, clean, attribution = await self._prepare(
            team_id, "images", clean, ModelType.IMAGE, api_key_id
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "images",
            clean,
            lambda: self._gateway.aimages(clean, model, values),
            reservation,
            attribution=attribution,
        )
