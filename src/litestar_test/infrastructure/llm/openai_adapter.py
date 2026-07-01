"""OpenAI-compatible adapters (OpenAI, Databricks; Azure subclasses the base).

The request is already OpenAI-shaped, so this is mostly a passthrough: we merge
the model's default `params`, point `model` at the upstream id, build the SDK
client from the credential, and return the response as a dict. Subclasses only
provide the client constructor; the four operations are shared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI, OpenAI

from litestar_test.domain.entities import Model
from litestar_test.domain.exceptions import CredentialMisconfigured
from litestar_test.infrastructure.llm.resilience import ResilienceConfig


def require_api_key(credentials: dict[str, str]) -> str:
    api_key = credentials.get("api_key")
    if not api_key:
        raise CredentialMisconfigured("credential is missing 'api_key'")
    return api_key


# Operation shapes the plain OpenAI provider supports.
SUPPORTED = frozenset({"chat.completions", "responses"})


def _kwargs(request: dict[str, Any], model: Model) -> dict[str, Any]:
    # Model params are defaults; the incoming request overrides them.
    merged = {**model.params, **request}
    merged["model"] = model.provider_model_id  # alias -> upstream model id (or deployment)
    return merged


def _base_url(credentials: dict[str, str]) -> str | None:
    # Endpoint comes only from the (admin-managed) credential, never from the
    # team-controlled model — otherwise a team admin could point the base URL at
    # an arbitrary host and exfiltrate the credential's secret.
    return credentials.get("api_base")


class OpenAICompatibleAdapter:
    """Shared operations for any client exposing the OpenAI SDK surface."""

    def __init__(self, resilience: ResilienceConfig | None = None) -> None:
        self._resilience = resilience or ResilienceConfig()

    def _sync_client(self, model: Model, credentials: dict[str, str]) -> Any:
        raise NotImplementedError

    def _async_client(self, model: Model, credentials: dict[str, str]) -> Any:
        raise NotImplementedError

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._sync_client(model, credentials)
        return client.chat.completions.create(**_kwargs(request, model)).model_dump()

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._async_client(model, credentials)
        result = await client.chat.completions.create(**_kwargs(request, model))
        return result.model_dump()

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._sync_client(model, credentials)
        return client.responses.create(**_kwargs(request, model)).model_dump()

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._async_client(model, credentials)
        result = await client.responses.create(**_kwargs(request, model))
        return result.model_dump()

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        client = self._async_client(model, credentials)
        kwargs = _kwargs(request, model)
        kwargs["stream"] = True
        # Any: with stream=True the SDK returns AsyncStream (no model_dump itself);
        # each yielded chunk is a ChatCompletionChunk that does have model_dump.
        stream: Any = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            yield chunk.model_dump()

    async def astream_responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        client = self._async_client(model, credentials)
        kwargs = _kwargs(request, model)
        kwargs["stream"] = True
        stream: Any = await client.responses.create(**kwargs)
        async for event in stream:
            yield event.model_dump()

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._sync_client(model, credentials)
        return client.embeddings.create(**_kwargs(request, model)).model_dump()

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._async_client(model, credentials)
        result = await client.embeddings.create(**_kwargs(request, model))
        return result.model_dump()

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._sync_client(model, credentials)
        return client.images.generate(**_kwargs(request, model)).model_dump()

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._async_client(model, credentials)
        result = await client.images.generate(**_kwargs(request, model))
        return result.model_dump()


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Plain OpenAI, and OpenAI-compatible endpoints (e.g. Databricks via base_url)."""

    def _sync_client(self, model: Model, credentials: dict[str, str]) -> OpenAI:
        return OpenAI(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )

    def _async_client(self, model: Model, credentials: dict[str, str]) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )
