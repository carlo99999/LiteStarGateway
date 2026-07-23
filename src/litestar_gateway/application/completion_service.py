"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials. Everything money-side —
budget admission, usage metering, billing, traces — is delegated to `UsageMeter`.
"""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.callable_aliases import CallableAliasResolver, ResolvedCallable
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import Model, ModelType, Provider, UsageAttribution
from litestar_gateway.domain.exceptions import (
    CredentialNotFound,
    ModelDisabled,
    ModelNotFound,
    ModelTypeMismatch,
    ProviderMismatch,
    UnsupportedOperation,
    UpstreamResponseInvalid,
)
from litestar_gateway.domain.ports import (
    CredentialRepository,
    LLMGateway,
    ModelRepository,
)
from litestar_gateway.domain.request_policy import (
    clamp_native_output_tokens,
    clamp_output_tokens,
    native_reservation_view,
    reject_native_control_kwargs,
    sanitize_request,
    validate_responses_request,
)
from litestar_gateway.domain.routing import CandidateModel, RouterConfig

RequestValidator = Callable[[Model, dict[str, Any]], dict[str, Any]]


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
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._meter = meter
        self._router_service = router_service
        self._callable_resolver = callable_resolver

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
        reservation: float = 0.0,
        settle_view: Callable[[dict[str, Any]], dict[str, Any]] = lambda response: response,
        attribution: UsageAttribution | None = None,
    ) -> dict[str, Any]:
        """Run one gateway call, observing success (usage + trace) and failure
        (error trace) before the exception propagates to the HTTP layer. The
        budget reservation taken at admission is released either way. The request
        is passed to settlement so usage can be estimated if the provider
        reported none (H14). `settle_view` maps the raw response to the usage-only
        shape settlement reads (identity for OpenAI-shaped responses; the native
        Gemini path passes `_gemini_usage`), so the raw body is still returned to
        the caller verbatim while billing sees the native token counts."""
        start = perf_counter()
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
            await self._meter.settle_ok(
                team_id,
                api_key_id,
                model,
                operation,
                settle_view(response),
                latency_ms,
                request,
                attribution,
            )
            await self._attach_routing_usage(response)
            return response
        finally:
            self._meter.release(team_id, reservation)

    async def _attach_routing_usage(self, response: dict[str, Any]) -> None:
        """Savings observability (§7): give the routing decision, if one was
        made for this request, its actual token usage. Streams are settled
        inside the meter and are not attached in this phase."""
        if self._router_service is None:
            return
        usage = response.get("usage") or {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(prompt, int) and isinstance(completion, int):
            await self._router_service.record_usage(prompt, completion)

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
            self._meter.release(team_id, reservation)
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
        reservation: float,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Native mirror of `_metered`: wrap the raw Anthropic stream in the native
        metered generator (usage accumulated from the raw events) and release the
        budget reservation exactly once — the generator's finally when iterated, a
        `weakref.finalize` for the never-iterated (drop-before-first-byte) case."""
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._meter.release(team_id, reservation)

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
        weakref.finalize(gen, release)
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
            self._meter.release(team_id, reservation)
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
        reservation: float,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Native mirror of `_metered_native` for the Gemini wire shape: wrap the raw
        Gemini chunk stream in the native metered generator (usage accumulated from
        the raw `usageMetadata`) and release the budget reservation exactly once —
        the generator's finally when iterated, a `weakref.finalize` for the
        never-iterated (drop-before-first-byte) case."""
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._meter.release(team_id, reservation)

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
        weakref.finalize(gen, release)
        return gen

    async def _prepare(
        self,
        team_id: UUID,
        operation: str,
        request: dict[str, Any],
        expected_type: ModelType,
        api_key_id: UUID | None,
        request_validator: RequestValidator | None = None,
    ) -> tuple[Model, dict[str, str], float, dict[str, Any], UsageAttribution]:
        # Gate the caller before router strategies: judge/embedding strategies
        # may make billable provider calls while resolving a virtual model.
        # The later admit handles team RPM + budget and omits the key so this
        # external request consumes exactly one key-RPM hit.
        await self._meter.enforce_key_rate_limit(api_key_id)
        alias = request.get("model")
        model = None
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
                model = await self._models.get_by_name(team_id, decision.model_name)
        model = self._ensure_usable(model, alias, expected_type)
        _reject_unsupported_n(operation, model, request)
        # Per-model output ceiling: clamp/inject now that the model is known, and
        # reserve from the clamped request so admission and the provider call agree.
        clean = clamp_output_tokens(operation, request, model.max_output_tokens)
        if request_validator is not None:
            clean = request_validator(model, clean)
        reservation = await self._meter.admit(team_id, model, clean)
        try:
            values = await self._credentials.get_values(model.credential_id)
            if values is None:
                raise CredentialNotFound(str(model.credential_id))
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        attribution = self._usage_attribution(team_id, alias, model, resolved)
        return model, values, reservation, clean, attribution

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID | None, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("chat.completions", request)
        model, values, reservation, clean, attribution = await self._prepare(
            team_id, "chat.completions", clean, ModelType.CHAT, api_key_id
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
        )

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
        )

    async def open_chat_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts, metered for usage. Awaited before
        streaming starts so resolution errors surface as HTTP status codes."""
        clean = sanitize_request("chat.completions", request)
        model, values, reservation, clean, attribution = await self._prepare(
            team_id, "chat.completions", clean, ModelType.CHAT, api_key_id
        )
        try:
            stream = await self._gateway.astream_chat_completion(clean, model, values)
        except BaseException:
            self._meter.release(team_id, reservation)
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
            self._meter.release(team_id, reservation)
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
        reservation: float,
        attribution: UsageAttribution,
    ) -> AsyncIterator[dict[str, Any]]:
        """Wrap the provider stream with usage metering, releasing the budget
        reservation exactly once. The metered generator releases it in its
        finally when iterated; a `weakref.finalize` covers the case where the
        SSE layer returns without ever starting it (client drops before the
        first byte) — otherwise the reservation would leak into InFlightSpend
        forever and eventually 402 the whole team (M27)."""
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._meter.release(team_id, reservation)

        gen = self._meter.metered_stream(
            team_id, api_key_id, model, operation, stream, request, release, attribution
        )
        weakref.finalize(gen, release)
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
