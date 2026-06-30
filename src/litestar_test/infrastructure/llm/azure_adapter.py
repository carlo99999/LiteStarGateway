"""Azure OpenAI adapter — same OpenAI surface, different client construction.

Credential `values`: `api_key`, `api_base` (the Azure endpoint), `api_version`.
The model's `provider_model_id` is the Azure *deployment* name.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncAzureOpenAI, AzureOpenAI

from litestar_test.domain.entities import Model
from litestar_test.infrastructure.llm.openai_adapter import OpenAICompatibleAdapter


def _client_kwargs(model: Model, credentials: dict[str, str]) -> dict[str, Any]:
    return {
        "api_key": credentials["api_key"],
        "azure_endpoint": credentials.get("api_base") or model.api_base,
        "api_version": credentials.get("api_version") or model.api_version,
    }


class AzureOpenAIAdapter(OpenAICompatibleAdapter):
    def _sync_client(self, model: Model, credentials: dict[str, str]) -> AzureOpenAI:
        return AzureOpenAI(**_client_kwargs(model, credentials))

    def _async_client(self, model: Model, credentials: dict[str, str]) -> AsyncAzureOpenAI:
        return AsyncAzureOpenAI(**_client_kwargs(model, credentials))
