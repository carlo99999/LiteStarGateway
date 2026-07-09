"""Port — LLM provider gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from litestar_gateway.domain.entities import Model


@runtime_checkable
class LLMGateway(Protocol):
    """Port for calling LLM providers in an OpenAI-compatible way.

    Takes an OpenAI-shaped `request`, the resolved `model` (provider, upstream id,
    params, endpoint overrides) and the decrypted credential `values`. Returns an
    OpenAI-shaped response dict. Sync and async variants mirror the OpenAI SDK;
    the sync ones block and must never be called from an async handler.
    """

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve eagerly (await) and return an async iterator of OpenAI
        `chat.completion.chunk` dicts. Resolution errors surface before streaming."""
        ...

    async def astream_responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve eagerly and return an async iterator of Responses-API stream
        event dicts (each carries a `type`)."""
        ...

    async def anative_messages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        """Provider-native Anthropic Messages passthrough: send the native request
        body upstream unchanged and return the native response dict verbatim (no
        OpenAI translation). `model` resolves the upstream model id + capability."""
        ...

    async def astream_native_messages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Provider-native Anthropic Messages streaming passthrough: resolve eagerly
        and relay the raw Anthropic stream events verbatim (each carries a `type`),
        with NO OpenAI translation. Only open-time/iteration errors are normalized to
        domain errors; the events themselves flow through untouched."""
        ...

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]: ...
