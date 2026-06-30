"""Anthropic adapter: translates OpenAI chat.completions ↔ Anthropic Messages.

Pure translators (`to_anthropic_request` / `from_anthropic_response`) do the
schema work; the adapter is a thin client wrapper. Responses are provided by
wrapping this adapter in `ChatToResponsesAdapter`.

First-cut scope: text-in/text-out. Not yet translated: tool/function calling,
multimodal content, structured outputs.
"""

from __future__ import annotations

import time
from typing import Any

from anthropic import Anthropic, AsyncAnthropic

from litestar_test.domain.entities import Model

# Anthropic requires max_tokens; use this when the request omits it.
DEFAULT_MAX_TOKENS = 1024

_FINISH_REASON = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)
        )
    return ""


def to_anthropic_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = {**model.params, **request}

    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in effective.get("messages", []):
        role = message.get("role")
        text = _text(message.get("content"))
        if role == "system":
            system_parts.append(text)
        elif role in ("user", "assistant"):
            messages.append({"role": role, "content": text})
        # tool/function messages are ignored in this first cut

    kwargs: dict[str, Any] = {
        "model": model.provider_model_id,
        "messages": messages,
        "max_tokens": effective.get("max_tokens")
        or effective.get("max_completion_tokens")
        or DEFAULT_MAX_TOKENS,
    }
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p"):
        if key in effective:
            kwargs[key] = effective[key]
    if (stop := effective.get("stop")) is not None:
        kwargs["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    return kwargs


def from_anthropic_response(message: dict[str, Any]) -> dict[str, Any]:
    text = "".join(
        block.get("text", "")
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    usage = message.get("usage") or {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    return {
        "id": message.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": message.get("model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _FINISH_REASON.get(message.get("stop_reason", ""), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": (input_tokens or 0) + (output_tokens or 0),
        },
    }


def _base_url(model: Model, credentials: dict[str, str]) -> str | None:
    return model.api_base or credentials.get("api_base")


class AnthropicAdapter:
    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = Anthropic(api_key=credentials["api_key"], base_url=_base_url(model, credentials))
        message: Any = client.messages.create(**to_anthropic_request(request, model))
        return from_anthropic_response(message.model_dump())

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = AsyncAnthropic(
            api_key=credentials["api_key"], base_url=_base_url(model, credentials)
        )
        message: Any = await client.messages.create(**to_anthropic_request(request, model))
        return from_anthropic_response(message.model_dump())
