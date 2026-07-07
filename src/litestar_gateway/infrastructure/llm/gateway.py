"""Gateway: routes an OpenAI-shaped call to the right provider adapter.

A capability matrix declares which (provider, operation) pairs are supported;
unsupported combinations raise `UnsupportedOperation` (→ 501) instead of leaking
a provider error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from litestar_gateway.domain.entities import Model, Provider
from litestar_gateway.domain.exceptions import UnsupportedOperation
from litestar_gateway.infrastructure.llm.anthropic_adapter import AnthropicAdapter
from litestar_gateway.infrastructure.llm.azure_adapter import AzureOpenAIAdapter
from litestar_gateway.infrastructure.llm.bedrock_adapter import BedrockAdapter
from litestar_gateway.infrastructure.llm.errors import (
    arun_translated,
    run_translated,
    translate_stream,
)
from litestar_gateway.infrastructure.llm.openai_adapter import OpenAIAdapter
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.llm.responses_emulation import ChatToResponsesAdapter
from litestar_gateway.infrastructure.llm.vertex_adapter import VertexAdapter

_CHAT = "chat.completions"
_RESPONSES = "responses"
_EMBEDDINGS = "embeddings"
_IMAGES = "image_generation"


class LLMGatewayImpl:
    def __init__(self, resilience: ResilienceConfig | None = None) -> None:
        resilience = resilience or ResilienceConfig()
        openai_adapter = OpenAIAdapter(resilience)  # OpenAI + Databricks share the client surface
        # provider -> (adapter, supported operation shapes)
        self._registry = {
            Provider.OPENAI: (
                openai_adapter,
                frozenset({_CHAT, _RESPONSES, _EMBEDDINGS, _IMAGES}),
            ),
            Provider.AZURE_OPENAI: (
                AzureOpenAIAdapter(resilience),
                frozenset({_CHAT, _RESPONSES, _EMBEDDINGS, _IMAGES}),
            ),
            # Databricks: no native Responses API (emulated); embeddings are OpenAI-compatible.
            Provider.DATABRICKS: (
                ChatToResponsesAdapter(openai_adapter),
                frozenset({_CHAT, _RESPONSES, _EMBEDDINGS}),
            ),
            # Anthropic: chat + emulated Responses. No embeddings API.
            Provider.ANTHROPIC: (
                ChatToResponsesAdapter(AnthropicAdapter(resilience)),
                frozenset({_CHAT, _RESPONSES}),
            ),
            # Vertex/Gemini: chat + emulated Responses + embeddings + images (Imagen).
            Provider.VERTEX_AI: (
                ChatToResponsesAdapter(VertexAdapter(resilience)),
                frozenset({_CHAT, _RESPONSES, _EMBEDDINGS, _IMAGES}),
            ),
            # Bedrock: Converse chat + emulated Responses + invoke_model
            # embeddings (Titan/Cohere) and images (Titan Image Generator).
            Provider.BEDROCK: (
                ChatToResponsesAdapter(BedrockAdapter(resilience)),
                frozenset({_CHAT, _RESPONSES, _EMBEDDINGS, _IMAGES}),
            ),
        }

    def _resolve(self, provider: Provider, operation: str) -> Any:
        entry = self._registry.get(provider)
        if entry is None:
            raise UnsupportedOperation(f"Provider '{provider}' is not supported yet")
        adapter, supported = entry
        if operation not in supported:
            raise UnsupportedOperation(f"Provider '{provider}' does not support '{operation}'")
        return adapter

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "chat.completions")
        return run_translated(lambda: adapter.chat_completion(request, model, credentials))

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "chat.completions")
        return await arun_translated(adapter.achat_completion(request, model, credentials))

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "responses")
        return run_translated(lambda: adapter.responses(request, model, credentials))

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "responses")
        return await arun_translated(adapter.aresponses(request, model, credentials))

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # Resolve eagerly (await) so capability errors surface before streaming.
        adapter = self._resolve(model.provider, "chat.completions")
        return translate_stream(adapter.astream_chat_completion(request, model, credentials))

    async def astream_responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        adapter = self._resolve(model.provider, "responses")
        return translate_stream(adapter.astream_responses(request, model, credentials))

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "embeddings")
        return run_translated(lambda: adapter.embeddings(request, model, credentials))

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "embeddings")
        return await arun_translated(adapter.aembeddings(request, model, credentials))

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "image_generation")
        return run_translated(lambda: adapter.images(request, model, credentials))

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "image_generation")
        return await arun_translated(adapter.aimages(request, model, credentials))
