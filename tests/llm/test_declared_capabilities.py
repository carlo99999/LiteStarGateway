"""Plan 18 Phase 2 — per-model declared capabilities.

One `openai_compatible` credential may front a chat-only Ollama and another a
vLLM that also serves embeddings, so a static per-provider operation set cannot
express what a given backend does. The model declares it; the gateway never
probes upstream to find out.

The six pre-existing providers keep their provider-declared sets untouched —
that non-regression is the point of the last class here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.entities.model import (
    DECLARABLE_CAPABILITIES,
    DEFAULT_CAPABILITIES,
)
from litestar_gateway.domain.exceptions import UnsupportedOperation
from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl


def _model(provider: Provider, capabilities: frozenset[str] = DEFAULT_CAPABILITIES) -> Model:
    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=provider,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="upstream-id",
        params={},
        api_version=None,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        enabled=True,
        created_at=datetime.now(UTC),
        capabilities=capabilities,
    )


class TestDefaults:
    def test_a_model_defaults_to_chat_only(self) -> None:
        # Fail-closed: an under-declared model serves less, never more.
        assert _model(Provider.OPENAI_COMPATIBLE).capabilities == DEFAULT_CAPABILITIES

    def test_the_declarable_set_mirrors_the_gateway_operations(self) -> None:
        assert DECLARABLE_CAPABILITIES == {
            "chat.completions",
            "embeddings",
            "image_generation",
        }


class TestIntersection:
    def test_a_declared_operation_resolves(self) -> None:
        gateway = LLMGatewayImpl()
        model = _model(Provider.OPENAI_COMPATIBLE, frozenset({"chat.completions", "embeddings"}))
        assert gateway._resolve(model, "embeddings") is not None

    def test_an_undeclared_operation_is_refused(self) -> None:
        gateway = LLMGatewayImpl()
        model = _model(Provider.OPENAI_COMPATIBLE, frozenset({"chat.completions"}))
        with pytest.raises(UnsupportedOperation, match="embeddings"):
            gateway._resolve(model, "embeddings")

    def test_declaring_more_than_the_adapter_offers_grants_nothing(self) -> None:
        # The declaration narrows the provider's maximum set; it can never widen
        # it. `responses` is not something this adapter serves at all.
        gateway = LLMGatewayImpl()
        model = _model(Provider.OPENAI_COMPATIBLE, frozenset({"responses"}))
        with pytest.raises(UnsupportedOperation):
            gateway._resolve(model, "responses")


class TestOtherProvidersAreUnaffected:
    """The declaration constrains `openai_compatible` only. Every other provider
    keeps the static set it advertised before this phase, whatever a stray
    `capabilities` value on the row happens to say."""

    @pytest.mark.parametrize(
        ("provider", "operation"),
        [
            (Provider.OPENAI, "embeddings"),
            (Provider.OPENAI, "image_generation"),
            (Provider.AZURE_OPENAI, "responses"),
            (Provider.VERTEX_AI, "embeddings"),
            (Provider.BEDROCK, "image_generation"),
            (Provider.ANTHROPIC, "chat.completions"),
        ],
    )
    def test_provider_declared_operations_still_resolve(
        self, provider: Provider, operation: str
    ) -> None:
        gateway = LLMGatewayImpl()
        # Chat-only capabilities, which would refuse these were the intersection
        # applied to every provider.
        model = _model(provider, frozenset({"chat.completions"}))
        assert gateway._resolve(model, operation) is not None

    def test_an_operation_the_provider_lacks_is_still_refused(self) -> None:
        gateway = LLMGatewayImpl()
        model = _model(Provider.ANTHROPIC, DECLARABLE_CAPABILITIES)
        with pytest.raises(UnsupportedOperation):
            gateway._resolve(model, "embeddings")
