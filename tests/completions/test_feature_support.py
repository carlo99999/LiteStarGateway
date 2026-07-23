"""Fail-loud feature boundaries for translated provider Chat adapters.

Anthropic supports governed non-streaming tools; Vertex/Bedrock tools and
non-text content on all three translators remain explicit 501s.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import UnsupportedOperation
from litestar_gateway.infrastructure.llm.anthropic_adapter import to_anthropic_request
from litestar_gateway.infrastructure.llm.bedrock_adapter import to_converse_request
from litestar_gateway.infrastructure.llm.vertex_adapter import to_gemini_request

TRANSLATORS = [
    (to_anthropic_request, Provider.ANTHROPIC, "claude-3-5-sonnet"),
    (to_gemini_request, Provider.VERTEX_AI, "gemini-1.5-pro"),
    (to_converse_request, Provider.BEDROCK, "anthropic.claude-3-5-sonnet-v2:0"),
]
TOOL_UNSUPPORTED_TRANSLATORS = TRANSLATORS[1:]


def _model(provider: Provider, provider_model_id: str) -> Model:
    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=provider,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id=provider_model_id,
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
IMAGE_MESSAGE = {
    "role": "user",
    "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ],
}


@pytest.mark.parametrize("translate,provider,model_id", TOOL_UNSUPPORTED_TRANSLATORS)
def test_tools_are_rejected(translate, provider, model_id) -> None:
    with pytest.raises(UnsupportedOperation):
        translate(
            {"messages": [{"role": "user", "content": "hi"}], "tools": TOOLS},
            _model(provider, model_id),
        )


@pytest.mark.parametrize("translate,provider,model_id", TOOL_UNSUPPORTED_TRANSLATORS)
def test_tool_choice_is_rejected(translate, provider, model_id) -> None:
    with pytest.raises(UnsupportedOperation):
        translate(
            {"messages": [{"role": "user", "content": "hi"}], "tool_choice": "auto"},
            _model(provider, model_id),
        )


@pytest.mark.parametrize("translate,provider,model_id", TRANSLATORS)
def test_image_content_is_rejected(translate, provider, model_id) -> None:
    with pytest.raises(UnsupportedOperation):
        translate({"messages": [IMAGE_MESSAGE]}, _model(provider, model_id))


@pytest.mark.parametrize("translate,provider,model_id", TRANSLATORS)
def test_plain_text_still_translates(translate, provider, model_id) -> None:
    # Regression: the guard must not reject an ordinary text request.
    kwargs = translate(
        {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
        _model(provider, model_id),
    )
    assert kwargs  # a dict of provider kwargs, no exception


@pytest.mark.parametrize("translate,provider,model_id", TRANSLATORS)
def test_structured_output_still_translates(translate, provider, model_id) -> None:
    # Regression: response_format (structured output) is not tool-calling and
    # must keep working — it is translated internally, not rejected.
    kwargs = translate(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            },
        },
        _model(provider, model_id),
    )
    assert kwargs
