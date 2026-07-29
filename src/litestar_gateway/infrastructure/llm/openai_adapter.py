"""OpenAI-compatible adapters (OpenAI, Databricks; Azure subclasses the base).

The request is already OpenAI-shaped, so this is mostly a passthrough: we merge
the model's default `params`, point `model` at the upstream id, build the SDK
client from the credential, and return the response as a dict. Subclasses only
provide the client constructor; the four operations are shared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from openai import AsyncOpenAI, BadRequestError, OpenAI

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import CredentialMisconfigured
from litestar_gateway.infrastructure.llm.client_registry import (
    ClientKey,
    ClientRegistry,
    fingerprint_material,
)
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig


def _swap_max_tokens(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Reasoning models (o1/gpt-5-family) reject `max_tokens` and require
    `max_completion_tokens`. Return a copy with the swap, or None if there's
    nothing to swap (already using the new field, or neither present)."""
    if "max_tokens" in kwargs and "max_completion_tokens" not in kwargs:
        swapped = dict(kwargs)
        swapped["max_completion_tokens"] = swapped.pop("max_tokens")
        return swapped
    return None


def _is_max_tokens_error(exc: BadRequestError) -> bool:
    message = str(getattr(exc, "message", "") or exc).lower()
    return "max_tokens" in message and "max_completion_tokens" in message


def _chat_create(client: Any, kwargs: dict[str, Any]) -> Any:
    """chat.completions.create, retrying once with max_completion_tokens when the
    provider rejects max_tokens (reasoning models). Non-reasoning models and
    other providers never hit the retry."""
    try:
        return client.chat.completions.create(**kwargs)
    except BadRequestError as exc:
        swapped = _swap_max_tokens(kwargs)
        if swapped is None or not _is_max_tokens_error(exc):
            raise
        return client.chat.completions.create(**swapped)


async def _achat_create(client: Any, kwargs: dict[str, Any]) -> Any:
    try:
        return await client.chat.completions.create(**kwargs)
    except BadRequestError as exc:
        swapped = _swap_max_tokens(kwargs)
        if swapped is None or not _is_max_tokens_error(exc):
            raise
        return await client.chat.completions.create(**swapped)


def require_api_key(credentials: dict[str, str]) -> str:
    api_key = credentials.get("api_key")
    if not api_key:
        raise CredentialMisconfigured("credential is missing 'api_key'")
    return api_key


# Operation shapes the plain OpenAI provider supports.
SUPPORTED = frozenset({"chat.completions", "responses"})


def _kwargs(request: dict[str, Any], model: Model) -> dict[str, Any]:
    # `params` are defaults the client may override; `params_enforced` is admin
    # policy applied last (client cannot override). See Model.merge_params.
    merged = model.merge_params(request)
    merged["model"] = model.provider_model_id  # alias -> upstream model id (or deployment)
    return merged


def _base_url(credentials: dict[str, str]) -> str | None:
    # Endpoint comes only from the (admin-managed) credential, never from the
    # team-controlled model — otherwise a team admin could point the base URL at
    # an arbitrary host and exfiltrate the credential's secret.
    return credentials.get("api_base")


class OpenAICompatibleAdapter:
    """Shared operations for any client exposing the OpenAI SDK surface."""

    def __init__(
        self,
        resilience: ResilienceConfig | None = None,
        client_registry: ClientRegistry | None = None,
    ) -> None:
        self._resilience = resilience or ResilienceConfig()
        # None keeps the old construct-and-close-per-call behavior (used by
        # standalone adapter construction, e.g. in unit tests); LLMGatewayImpl
        # always supplies one in the running gateway.
        self._client_registry = client_registry

    def _sync_client(self, model: Model, credentials: dict[str, str]) -> Any:
        raise NotImplementedError

    def _async_client(self, model: Model, credentials: dict[str, str]) -> Any:
        raise NotImplementedError

    def _client_key(self, model: Model, credentials: dict[str, str]) -> ClientKey:
        raise NotImplementedError

    def _run(
        self, model: Model, credentials: dict[str, str], call: Callable[[Any], Any]
    ) -> dict[str, Any]:
        # Each SDK client owns an httpx connection pool; close it after the call so
        # per-request clients don't leak sockets/file descriptors. The sync path
        # isn't on the async hot path profiled in Plan 14 and doesn't lease.
        client = self._sync_client(model, credentials)
        try:
            return call(client).model_dump()
        finally:
            client.close()

    @asynccontextmanager
    async def _leased_async_client(
        self, model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[Any]:
        """Reuse a cached async client when a registry is wired in; otherwise
        construct-and-close exactly like before (unchanged fallback)."""
        if self._client_registry is None:
            client = self._async_client(model, credentials)
            try:
                yield client
            finally:
                await client.close()
            return
        key = self._client_key(model, credentials)
        async with self._client_registry.lease(
            key, lambda: self._async_client(model, credentials)
        ) as client:
            yield client

    async def _arun(
        self, model: Model, credentials: dict[str, str], call: Callable[[Any], Awaitable[Any]]
    ) -> dict[str, Any]:
        async with self._leased_async_client(model, credentials) as client:
            return (await call(client)).model_dump()

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._run(model, credentials, lambda c: _chat_create(c, _kwargs(request, model)))

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._arun(
            model, credentials, lambda c: _achat_create(c, _kwargs(request, model))
        )

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._run(
            model, credentials, lambda c: c.responses.create(**_kwargs(request, model))
        )

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._arun(
            model, credentials, lambda c: c.responses.create(**_kwargs(request, model))
        )

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # The client is leased for the whole stream and released (not force-
        # closed while another request still holds it) once iteration ends or
        # the client disconnects — `_leased_async_client`'s `__aexit__` still
        # runs on generator close/cancellation.
        async with self._leased_async_client(model, credentials) as client:
            kwargs = _kwargs(request, model)
            kwargs["stream"] = True
            # Ask for the final usage chunk so the gateway can meter streamed
            # calls (OpenAI omits usage from streams unless this is set).
            # Forced on: billing must not depend on the client opting in.
            kwargs["stream_options"] = {**kwargs.get("stream_options", {}), "include_usage": True}
            # Any: with stream=True the SDK returns AsyncStream (no model_dump
            # itself); each yielded chunk is a ChatCompletionChunk that does.
            stream: Any = await _achat_create(client, kwargs)
            async for chunk in stream:
                yield chunk.model_dump()

    async def astream_responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._leased_async_client(model, credentials) as client:
            kwargs = _kwargs(request, model)
            kwargs["stream"] = True
            stream: Any = await client.responses.create(**kwargs)
            async for event in stream:
                yield event.model_dump()

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._run(
            model, credentials, lambda c: c.embeddings.create(**_kwargs(request, model))
        )

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._arun(
            model, credentials, lambda c: c.embeddings.create(**_kwargs(request, model))
        )

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._run(model, credentials, lambda c: c.images.generate(**_kwargs(request, model)))

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._arun(
            model, credentials, lambda c: c.images.generate(**_kwargs(request, model))
        )


def _openai_client_kwargs(credentials: dict[str, str]) -> dict[str, Any]:
    return {"api_key": require_api_key(credentials), "base_url": _base_url(credentials)}


# Sent when an `openai_compatible` credential carries no key. The SDK refuses an
# empty one, and an unauthenticated local server ignores whatever arrives; a
# server that does require auth answers 401, which `errors.py` translates. Not a
# secret, and deliberately recognizable in a request log.
PLACEHOLDER_API_KEY = "not-used"  # pragma: allowlist secret


def _compatible_client_kwargs(credentials: dict[str, str]) -> dict[str, Any]:
    return {
        "api_key": credentials.get("api_key") or PLACEHOLDER_API_KEY,
        "base_url": _base_url(credentials),
    }


class OpenAICompatibleProviderAdapter(OpenAICompatibleAdapter):
    """Any endpoint speaking the OpenAI wire protocol (Plan 18).

    Identical to `OpenAIAdapter` but for two things: the API key is optional,
    and the client-cache key is tagged with its own provider so a pooled client
    is never shared with plain OpenAI when endpoint and credential happen to
    match.

    **No vendor branches, ever.** A backend needing special-casing is by
    definition not OpenAI-compatible and belongs behind its own provider with
    its own official SDK (design §1).
    """

    def _sync_client(self, model: Model, credentials: dict[str, str]) -> OpenAI:
        return OpenAI(**_compatible_client_kwargs(credentials), **self._resilience.client_kwargs)

    def _async_client(self, model: Model, credentials: dict[str, str]) -> AsyncOpenAI:
        return AsyncOpenAI(
            **_compatible_client_kwargs(credentials), **self._resilience.async_client_kwargs
        )

    def _client_key(self, model: Model, credentials: dict[str, str]) -> ClientKey:
        kwargs = _compatible_client_kwargs(credentials)
        return ClientKey(
            provider="openai_compatible",
            fingerprint=fingerprint_material(*kwargs.values()),
            endpoint=kwargs.get("base_url") or "",
        )


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Plain OpenAI, and OpenAI-compatible endpoints (e.g. Databricks via base_url)."""

    def _sync_client(self, model: Model, credentials: dict[str, str]) -> OpenAI:
        return OpenAI(**_openai_client_kwargs(credentials), **self._resilience.client_kwargs)

    def _async_client(self, model: Model, credentials: dict[str, str]) -> AsyncOpenAI:
        return AsyncOpenAI(
            **_openai_client_kwargs(credentials), **self._resilience.async_client_kwargs
        )

    def _client_key(self, model: Model, credentials: dict[str, str]) -> ClientKey:
        kwargs = _openai_client_kwargs(credentials)
        return ClientKey(
            provider="openai",
            fingerprint=fingerprint_material(*kwargs.values()),
            endpoint=kwargs.get("base_url") or "",
        )
