"""Plan 18 Phase 1 — the `openai_compatible` provider value and its adapter.

The adapter itself is the shared `OpenAICompatibleAdapter` that already serves
OpenAI and Databricks; what is new is a provider value, a credential contract
whose only required field is the endpoint, and a client-cache key that cannot
alias with plain OpenAI.
"""

from __future__ import annotations

import pytest

from litestar_gateway.domain.credential_policy import validate_credential_values
from litestar_gateway.domain.entities import Provider
from litestar_gateway.domain.exceptions import CredentialMisconfigured, UnsupportedOperation
from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl
from litestar_gateway.infrastructure.llm.openai_adapter import (
    PLACEHOLDER_API_KEY,
    OpenAICompatibleProviderAdapter,
)

# Test fixtures, not credentials.
SUPPLIED_KEY = "real"  # pragma: allowlist secret
SHARED_KEY = "same"  # pragma: allowlist secret


class TestProviderValue:
    def test_the_provider_exists(self) -> None:
        assert Provider("openai_compatible") is Provider.OPENAI_COMPATIBLE

    def test_n_is_not_honored(self) -> None:
        # Compatible backends disagree (vLLM honors `n`, Ollama does not).
        # Claiming support would over-reserve budget by up to MAX_N x on a
        # backend that silently returns one completion (R7-M50), so the
        # fail-safe answer is the only defensible one until it is declared.
        assert not Provider.OPENAI_COMPATIBLE.honors_n


class TestCredentialContract:
    def test_api_base_is_required(self) -> None:
        with pytest.raises(CredentialMisconfigured, match="api_base"):
            validate_credential_values(Provider.OPENAI_COMPATIBLE, {"api_key": "k"})

    def test_api_key_is_optional(self) -> None:
        # Local servers (Ollama, llama.cpp) accept no key at all.
        validate_credential_values(
            Provider.OPENAI_COMPATIBLE, {"api_base": "http://vllm.internal:8000/v1"}
        )

    def test_unexpected_keys_are_refused(self) -> None:
        with pytest.raises(CredentialMisconfigured, match="unexpected"):
            validate_credential_values(
                Provider.OPENAI_COMPATIBLE,
                {"api_base": "http://vllm.internal:8000/v1", "region": "us-east-1"},
            )


class TestClientConstruction:
    def _model(self):
        from datetime import UTC, datetime
        from uuid import uuid4

        from litestar_gateway.domain.entities import Model, ModelType

        return Model(
            id=uuid4(),
            team_id=uuid4(),
            name="local-llama",
            provider=Provider.OPENAI_COMPATIBLE,
            credential_id=uuid4(),
            type=ModelType.CHAT,
            provider_model_id="llama-3.1-8b",
            params={},
            api_version=None,
            input_cost_per_token=0.0,
            output_cost_per_token=0.0,
            enabled=True,
            created_at=datetime.now(UTC),
        )

    def test_a_missing_api_key_becomes_a_placeholder(self) -> None:
        # The SDK rejects an empty key, so an unauthenticated local server needs
        # *something*. A fixed non-secret placeholder is a documented behavior,
        # not a fallback that could mask a misconfigured secret: a server that
        # does require auth answers 401, which the error translator handles.
        adapter = OpenAICompatibleProviderAdapter()
        client = adapter._async_client(self._model(), {"api_base": "http://vllm.internal:8000/v1"})
        assert client.api_key == PLACEHOLDER_API_KEY

    def test_a_supplied_api_key_is_used(self) -> None:
        adapter = OpenAICompatibleProviderAdapter()
        client = adapter._async_client(
            self._model(),
            {
                "api_base": "https://api.groq.test/openai/v1",
                "api_key": SUPPLIED_KEY,
            },
        )
        assert client.api_key == SUPPLIED_KEY

    def test_neither_client_follows_redirects(self) -> None:
        # A redirect is one hop out of the egress allowlist: the target of a 307
        # from an allowlisted endpoint is not what the operator authorized, so
        # whatever bound the allowlist provides would be void. The async client
        # inherits httpx's `False`; the sync constructor is handed the SDK's own
        # default client, which sets `follow_redirects=True` unless we pass one.
        adapter = OpenAICompatibleProviderAdapter()
        credentials = {"api_base": "https://allowed.test/v1"}
        assert adapter._async_client(self._model(), credentials)._client.follow_redirects is False
        assert adapter._sync_client(self._model(), credentials)._client.follow_redirects is False

    def test_the_client_key_cannot_alias_with_plain_openai(self) -> None:
        # Same endpoint, same key, different provider: the pooled client must
        # not be shared, or one provider's request would be served by the
        # other's configured client.
        from litestar_gateway.infrastructure.llm.openai_adapter import OpenAIAdapter

        credentials = {
            "api_base": "https://shared.test/v1",
            "api_key": SHARED_KEY,
        }
        compatible = OpenAICompatibleProviderAdapter()._client_key(self._model(), credentials)
        plain = OpenAIAdapter()._client_key(self._model(), credentials)
        assert compatible.provider == "openai_compatible"
        assert plain.provider == "openai"
        assert compatible != plain


class TestGatewayCapabilities:
    """A model that declares nothing serves chat and nothing else. The
    intersection itself is covered in `test_declared_capabilities.py`."""

    def test_chat_resolves_by_default(self) -> None:
        gateway = LLMGatewayImpl()
        assert gateway._resolve(TestClientConstruction()._model(), "chat.completions") is not None

    @pytest.mark.parametrize("operation", ["embeddings", "image_generation", "responses"])
    def test_undeclared_operations_are_refused(self, operation: str) -> None:
        gateway = LLMGatewayImpl()
        with pytest.raises(UnsupportedOperation):
            gateway._resolve(TestClientConstruction()._model(), operation)
