"""Anthropic adapter: translates OpenAI chat.completions ↔ Anthropic Messages.

Pure translators (`to_anthropic_request` / `from_anthropic_response`) do the
schema work; the adapter is a thin client wrapper. Responses are provided by
wrapping this adapter in `ChatToResponsesAdapter`.

First-cut scope: text-in/text-out. Not yet translated: tool/function calling,
multimodal content, structured outputs.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from anthropic import Anthropic, AsyncAnthropic

from litestar_gateway.domain.entities import Model
from litestar_gateway.infrastructure.llm.openai_adapter import require_api_key
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig

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
    effective = model.merge_params(request)

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


def anthropic_event_to_delta(event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Map one Anthropic stream event to an OpenAI chunk (delta, finish_reason).

    Returns (None, None) for events that produce no chunk (e.g. pings, block stops).
    """
    etype = event.get("type")
    if etype == "message_start":
        return {"role": "assistant"}, None
    if etype == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return {"content": delta.get("text", "")}, None
        return None, None
    if etype == "message_delta":
        reason = (event.get("delta") or {}).get("stop_reason")
        if reason:
            return {}, _FINISH_REASON.get(reason, "stop")
    return None, None


def _base_url(credentials: dict[str, str]) -> str | None:
    # Endpoint from the (admin-managed) credential only, never from the model.
    return credentials.get("api_base")


class AnthropicAdapter:
    def __init__(self, resilience: ResilienceConfig | None = None) -> None:
        self._resilience = resilience or ResilienceConfig()

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Close the client after the call so its httpx pool isn't leaked.
        client = Anthropic(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )
        try:
            message: Any = client.messages.create(**to_anthropic_request(request, model))
            return from_anthropic_response(message.model_dump())
        finally:
            client.close()

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = AsyncAnthropic(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )
        try:
            message: Any = await client.messages.create(**to_anthropic_request(request, model))
            return from_anthropic_response(message.model_dump())
        finally:
            await client.close()

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        client = AsyncAnthropic(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )
        base = {
            "id": "chatcmpl-anthropic",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model.provider_model_id,
        }
        kwargs: dict[str, Any] = {**to_anthropic_request(request, model), "stream": True}
        # Anthropic reports input tokens on message_start and cumulative output
        # tokens on message_delta; accumulate them and emit a trailing
        # OpenAI-style usage chunk so streamed calls can be metered.
        input_tokens = 0
        output_tokens = 0
        # Keep the client open for the whole stream; close on completion/disconnect.
        try:
            stream: Any = await client.messages.create(**kwargs)
            async for event in stream:
                raw = event.model_dump()
                if raw.get("type") == "message_start":
                    start_usage = (raw.get("message") or {}).get("usage") or {}
                    input_tokens = start_usage.get("input_tokens") or 0
                    output_tokens = start_usage.get("output_tokens") or 0
                elif raw.get("type") == "message_delta":
                    delta_usage = raw.get("usage") or {}
                    output_tokens = delta_usage.get("output_tokens") or output_tokens
                delta, finish = anthropic_event_to_delta(raw)
                if delta is None and finish is None:
                    continue
                yield {
                    **base,
                    "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                }
            yield {
                **base,
                "choices": [],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
        finally:
            await client.close()
