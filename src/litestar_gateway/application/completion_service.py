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
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType
from litestar_gateway.domain.exceptions import (
    CredentialNotFound,
    ModelDisabled,
    ModelNotFound,
    ModelTypeMismatch,
    UnsupportedOperation,
)
from litestar_gateway.domain.ports import (
    CredentialRepository,
    LLMGateway,
    ModelRepository,
)
from litestar_gateway.domain.request_policy import clamp_output_tokens, sanitize_request


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
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._meter = meter
        self._router_service = router_service

    async def _dispatch(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        request: dict[str, Any],
        call: Callable[[], Awaitable[dict[str, Any]]],
        reservation: float = 0.0,
    ) -> dict[str, Any]:
        """Run one gateway call, observing success (usage + trace) and failure
        (error trace) before the exception propagates to the HTTP layer. The
        budget reservation taken at admission is released either way. The request
        is passed to settlement so usage can be estimated if the provider
        reported none (H14)."""
        start = perf_counter()
        try:
            try:
                response = await call()
            except Exception as exc:
                self._meter.trace_error(
                    team_id, api_key_id, model, operation, (perf_counter() - start) * 1000, exc
                )
                raise
            latency_ms = (perf_counter() - start) * 1000
            await self._meter.settle_ok(
                team_id, api_key_id, model, operation, response, latency_ms, request
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

    async def prepare_native(
        self, team_id: UUID, expected_type: ModelType, request: dict[str, Any]
    ) -> tuple[Model, dict[str, str]]:
        """Resolve a provider-native request's `model` alias to a usable team
        `Model` plus its decrypted credentials.

        Runs the *same* enable/type/credential guards as `_prepare`, minus smart
        routing (native endpoints resolve one concrete same-protocol model) and
        budget admission (the native surface meters natively around its own
        dispatch). The upstream `base_url` still comes only from the credential
        (`get_values`), never from the client."""
        alias = request.get("model")
        model = self._ensure_usable(
            await self._models.get_by_name(team_id, alias) if alias else None,
            alias,
            expected_type,
        )
        values = await self._credentials.get_values(model.credential_id)
        if values is None:
            raise CredentialNotFound(str(model.credential_id))
        return model, values

    async def _prepare(
        self,
        team_id: UUID,
        operation: str,
        request: dict[str, Any],
        expected_type: ModelType,
        api_key_id: UUID | None = None,
    ) -> tuple[Model, dict[str, str], float, dict[str, Any]]:
        alias = request.get("model")
        model = await self._models.get_by_name(team_id, alias) if alias else None
        if (
            model is None
            and alias
            and self._router_service is not None
            and expected_type is ModelType.CHAT
        ):
            # Smart routing: the alias may name a router (virtual model). The
            # strategy only rewrites the model name; the rest of the pipeline
            # (clamping, budget admission, metering) runs on the chosen model.
            router = await self._router_service.get_enabled_by_name(team_id, alias)
            if router is not None:
                decision = await self._router_service.route(router, request, api_key_id=api_key_id)
                model = await self._models.get_by_name(team_id, decision.model_name)
        model = self._ensure_usable(model, alias, expected_type)
        _reject_unsupported_n(operation, model, request)
        # Per-model output ceiling: clamp/inject now that the model is known, and
        # reserve from the clamped request so admission and the provider call agree.
        clean = clamp_output_tokens(operation, request, model.max_output_tokens)
        reservation = await self._meter.admit(team_id, model, clean)
        try:
            values = await self._credentials.get_values(model.credential_id)
            if values is None:
                raise CredentialNotFound(str(model.credential_id))
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        return model, values, reservation, clean

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("chat.completions", request)
        model, values, reservation, clean = await self._prepare(
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
        )

    async def responses(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("responses", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "responses", clean, ModelType.CHAT, api_key_id
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "responses",
            clean,
            lambda: self._gateway.aresponses(clean, model, values),
            reservation,
        )

    async def open_chat_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts, metered for usage. Awaited before
        streaming starts so resolution errors surface as HTTP status codes."""
        clean = sanitize_request("chat.completions", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "chat.completions", clean, ModelType.CHAT, api_key_id
        )
        try:
            stream = await self._gateway.astream_chat_completion(clean, model, values)
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        gen = self._metered(
            team_id, api_key_id, model, "chat.completions", stream, clean, reservation
        )
        return await _prime(gen)

    async def open_responses_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve (may raise → HTTP error) and return an async iterator of
        Responses-API stream events, metered for usage."""
        clean = sanitize_request("responses", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "responses", clean, ModelType.CHAT, api_key_id
        )
        try:
            stream = await self._gateway.astream_responses(clean, model, values)
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        gen = self._metered(team_id, api_key_id, model, "responses", stream, clean, reservation)
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
            team_id, api_key_id, model, operation, stream, request, release
        )
        weakref.finalize(gen, release)
        return gen

    async def embeddings(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("embeddings", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "embeddings", clean, ModelType.EMBEDDINGS
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "embeddings",
            clean,
            lambda: self._gateway.aembeddings(clean, model, values),
            reservation,
        )

    async def images(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("images", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "images", clean, ModelType.IMAGE
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "images",
            clean,
            lambda: self._gateway.aimages(clean, model, values),
            reservation,
        )
