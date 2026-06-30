"""Gateway: routes an OpenAI-shaped call to the right provider adapter.

A capability matrix declares which (provider, operation) pairs are supported;
unsupported combinations raise `UnsupportedOperation` (→ 501) instead of leaking
a provider error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from litestar_test.domain.entities import Model, Provider
from litestar_test.domain.exceptions import UnsupportedOperation
from litestar_test.infrastructure.llm.anthropic_adapter import AnthropicAdapter
from litestar_test.infrastructure.llm.azure_adapter import AzureOpenAIAdapter
from litestar_test.infrastructure.llm.openai_adapter import OpenAIAdapter
from litestar_test.infrastructure.llm.responses_emulation import ChatToResponsesAdapter
from litestar_test.infrastructure.llm.vertex_adapter import VertexAdapter

_CHAT = "chat.completions"
_RESPONSES = "responses"


class LLMGatewayImpl:
    def __init__(self) -> None:
        openai_adapter = OpenAIAdapter()  # OpenAI + Databricks share the client surface
        # provider -> (adapter, supported operation shapes)
        self._registry = {
            Provider.OPENAI: (openai_adapter, frozenset({_CHAT, _RESPONSES})),
            Provider.AZURE_OPENAI: (AzureOpenAIAdapter(), frozenset({_CHAT, _RESPONSES})),
            # Databricks has no native Responses API; emulate it over chat.completions.
            Provider.DATABRICKS: (
                ChatToResponsesAdapter(openai_adapter),
                frozenset({_CHAT, _RESPONSES}),
            ),
            # Anthropic: native chat translation; Responses emulated over it.
            Provider.ANTHROPIC: (
                ChatToResponsesAdapter(AnthropicAdapter()),
                frozenset({_CHAT, _RESPONSES}),
            ),
            # Vertex/Gemini: native chat translation; Responses emulated over it.
            Provider.VERTEX_AI: (
                ChatToResponsesAdapter(VertexAdapter()),
                frozenset({_CHAT, _RESPONSES}),
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
        return adapter.chat_completion(request, model, credentials)

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "chat.completions")
        return await adapter.achat_completion(request, model, credentials)

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "responses")
        return adapter.responses(request, model, credentials)

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        adapter = self._resolve(model.provider, "responses")
        return await adapter.aresponses(request, model, credentials)

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # Resolve eagerly (await) so capability errors surface before streaming.
        adapter = self._resolve(model.provider, "chat.completions")
        return adapter.astream_chat_completion(request, model, credentials)
