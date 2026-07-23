"""Anthropic adapter: translates OpenAI chat.completions ↔ Anthropic Messages.

Pure translators (`to_anthropic_request` / `from_anthropic_response`) do the
schema work; the adapter is a thin client wrapper. Responses are provided by
wrapping this adapter in `ChatToResponsesAdapter`.

Scope: text-in/text-out, structured outputs (`response_format`) translated to a
forced tool (streaming included), and faithful non-streaming client tool calls.
Streaming client tools and multimodal content remain fail-closed.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from anthropic import Anthropic, AsyncAnthropic

from litestar_gateway.domain.chat_tool_policy import validate_chat_request
from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import UpstreamResponseInvalid
from litestar_gateway.infrastructure.llm.feature_support import ensure_translatable_chat_request
from litestar_gateway.infrastructure.llm.openai_adapter import require_api_key
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.llm.structured_output import parse_response_format

# Anthropic requires max_tokens; use this when the request omits it.
DEFAULT_MAX_TOKENS = 1024

_FINISH_REASON = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    # Structured-output tool responses are handled explicitly by
    # `from_anthropic_response`; real client tools become `tool_calls`.
    "tool_use": "stop",
}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)
        )
    return ""


def _tool_input(arguments: str) -> dict[str, Any]:
    # `validate_chat_request` already proved this is finite JSON and an object.
    value = json.loads(arguments)
    assert isinstance(value, dict)
    return value


def _anthropic_messages(messages: list[Any]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool":
            results: list[dict[str, Any]] = []
            current = message
            while True:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": current["tool_call_id"],
                        "content": current["content"],
                    }
                )
                if index >= len(messages):
                    break
                candidate = messages[index]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                current = candidate
                index += 1
            translated.append({"role": "user", "content": results})
            continue
        if role not in {"user", "assistant"}:
            continue
        text = _text(message.get("content"))
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            content: list[dict[str, Any]] = []
            if text:
                content.append({"type": "text", "text": text})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": _tool_input(call["function"]["arguments"]),
                }
                for call in tool_calls
            )
            translated.append({"role": "assistant", "content": content})
        else:
            translated.append({"role": role, "content": text})
    return translated


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    for tool in tools:
        function = tool["function"]
        parameters = function.get("parameters")
        mapped: dict[str, Any] = {
            "name": function["name"],
            "input_schema": parameters if parameters is not None else {"type": "object"},
        }
        for field in ("description", "strict"):
            if field in function:
                mapped[field] = function[field]
        translated.append(mapped)
    return translated


def _anthropic_tool_choice(effective: dict[str, Any]) -> dict[str, Any] | None:
    choice = effective.get("tool_choice")
    parallel = effective.get("parallel_tool_calls")
    mapped: dict[str, Any] | None = None
    if choice == "auto":
        mapped = {"type": "auto"}
    elif choice == "required":
        mapped = {"type": "any"}
    elif choice == "none":
        mapped = {"type": "none"}
    elif isinstance(choice, dict):
        mapped = {"type": "tool", "name": choice["function"]["name"]}
    elif parallel is not None:
        mapped = {"type": "auto"}
    if mapped is not None and parallel is not None and mapped["type"] != "none":
        mapped["disable_parallel_tool_use"] = not parallel
    return mapped


def to_anthropic_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    validate_chat_request(model, request)
    ensure_translatable_chat_request(effective, model.provider.value, allow_tools=True)
    structured = parse_response_format(effective)

    system_parts: list[str] = []
    for message in effective.get("messages", []):
        if isinstance(message, dict) and message.get("role") == "system":
            system_parts.append(_text(message.get("content")))
    messages = _anthropic_messages(effective.get("messages") or [])

    # json_object (no schema): Anthropic has no JSON mode, so nudge via system.
    if structured is not None and structured.schema is None:
        system_parts.append("Respond with a single valid JSON object and nothing else.")

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
    # json_schema: no native JSON mode — force a single tool whose input_schema is
    # the requested schema, so the model must return a matching tool_use block.
    # from_anthropic_response surfaces that input as the (JSON) message content.
    if structured is not None and structured.schema is not None:
        kwargs["tools"] = [
            {
                "name": structured.name,
                "description": "Return the result as JSON matching the schema.",
                "input_schema": structured.schema,
            }
        ]
        kwargs["tool_choice"] = {"type": "tool", "name": structured.name}
    elif isinstance(effective.get("tools"), list):
        kwargs["tools"] = _anthropic_tools(effective["tools"])
        if choice := _anthropic_tool_choice(effective):
            kwargs["tool_choice"] = choice
    return kwargs


def _billable_response(message: dict[str, Any]) -> dict[str, Any]:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return {"usage": {}}
    prompt = raw.get("input_tokens")
    completion = raw.get("output_tokens")
    if (
        not isinstance(prompt, int)
        or isinstance(prompt, bool)
        or prompt < 0
        or not isinstance(completion, int)
        or isinstance(completion, bool)
        or completion < 0
    ):
        return {"usage": {}}
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    return {"usage": usage}


def _invalid_response(message: dict[str, Any], detail: str) -> UpstreamResponseInvalid:
    return UpstreamResponseInvalid(detail, _billable_response(message))


def _validated_usage(message: dict[str, Any]) -> dict[str, int]:
    billable = _billable_response(message)["usage"]
    if not billable:
        raise _invalid_response(message, "Anthropic returned malformed usage")
    return billable


def _tool_calls(
    message: dict[str, Any],
    blocks: list[Any],
    expected_tool_names: set[str] | None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        call_id = block.get("id")
        name = block.get("name")
        tool_input = block.get("input")
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in seen_ids
            or not isinstance(name, str)
            or not name
            or (expected_tool_names is not None and name not in expected_tool_names)
            or not isinstance(tool_input, dict)
        ):
            raise _invalid_response(message, "Anthropic returned a malformed tool call")
        try:
            arguments = json.dumps(
                tool_input,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise _invalid_response(message, "Anthropic returned non-JSON tool arguments") from exc
        seen_ids.add(call_id)
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return calls


def from_anthropic_response(
    message: dict[str, Any],
    *,
    expected_tool_names: set[str] | None = None,
    require_tool_call: bool = False,
    max_tool_calls: int | None = None,
    structured_tool_name: str | None = None,
) -> dict[str, Any]:
    blocks = message.get("content", [])
    if not isinstance(blocks, list):
        raise _invalid_response(message, "Anthropic returned malformed content")
    stop_reason = message.get("stop_reason", "")
    if stop_reason == "pause_turn":
        raise _invalid_response(message, "Anthropic returned an incomplete paused turn")
    if not isinstance(stop_reason, str) or stop_reason not in _FINISH_REASON:
        raise _invalid_response(message, "Anthropic returned an invalid stop reason")
    usage = _validated_usage(message)
    if stop_reason == "refusal":
        # Classifier refusals invalidate every partial block. A refusal before
        # output reports token counts but Anthropic does not charge them.
        text = ""
        calls: list[dict[str, Any]] = []
        if usage["completion_tokens"] == 0:
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    else:
        text_parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in {"text", "tool_use"}:
                raise _invalid_response(message, "Anthropic returned malformed content")
            if block["type"] == "text":
                block_text = block.get("text")
                if not isinstance(block_text, str):
                    raise _invalid_response(message, "Anthropic returned malformed text content")
                text_parts.append(block_text)
        text = "".join(text_parts)
        allowed_names = expected_tool_names if structured_tool_name is None else None
        calls = _tool_calls(message, blocks, allowed_names)
        # Forced structured-output tool: serialize the input into content and
        # never expose the synthetic tool to the client.
        if structured_tool_name is not None:
            if (
                text_parts
                or len(calls) != 1
                or calls[0]["function"]["name"] != structured_tool_name
            ):
                raise _invalid_response(message, "Anthropic returned malformed structured output")
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    try:
                        text = json.dumps(
                            block.get("input") or {},
                            allow_nan=False,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    except (TypeError, ValueError) as exc:
                        raise _invalid_response(
                            message, "Anthropic returned malformed structured output"
                        ) from exc
                    break
            calls = []
        elif max_tool_calls is not None and len(calls) > max_tool_calls:
            raise _invalid_response(
                message, "Anthropic violated the requested parallel tool-call constraint"
            )
        elif require_tool_call and not calls:
            raise _invalid_response(message, "Anthropic omitted a required tool call")
        if structured_tool_name is None and bool(calls) != (stop_reason == "tool_use"):
            raise _invalid_response(message, "Anthropic returned an inconsistent tool response")
        if structured_tool_name is not None and stop_reason != "tool_use":
            raise _invalid_response(message, "Anthropic returned malformed structured output")
    chat_message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        chat_message["tool_calls"] = calls
    return {
        "id": message.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": message.get("model"),
        "choices": [
            {
                "index": 0,
                "message": chat_message,
                "finish_reason": "tool_calls" if calls else _FINISH_REASON.get(stop_reason, "stop"),
            }
        ],
        "usage": usage,
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
        dtype = delta.get("type")
        if dtype == "text_delta":
            return {"content": delta.get("text", "")}, None
        if dtype == "input_json_delta":
            # Structured output streams as a forced tool: relay its partial JSON
            # as content deltas so the client reconstructs the same JSON it gets
            # non-streamed (matching how OpenAI/Gemini stream JSON content).
            return {"content": delta.get("partial_json", "")}, None
        return None, None
    if etype == "message_delta":
        reason = (event.get("delta") or {}).get("stop_reason")
        if reason == "pause_turn":
            raise UpstreamResponseInvalid(
                "Anthropic returned an incomplete paused stream",
                {"usage": {}},
            )
        if reason:
            finish = _FINISH_REASON.get(reason)
            if finish is None:
                raise UpstreamResponseInvalid(
                    "Anthropic returned an invalid streaming stop reason",
                    {"usage": {}},
                )
            return {}, finish
    return None, None


def _base_url(credentials: dict[str, str]) -> str | None:
    # Endpoint from the (admin-managed) credential only, never from the model.
    return credentials.get("api_base")


def _response_tool_contract(
    request: dict[str, Any],
    model: Model,
) -> tuple[set[str], bool, int | None, str | None]:
    effective = model.merge_params(request)
    tools = effective.get("tools")
    expected_tool_names = (
        {
            tool["function"]["name"]
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
        if isinstance(tools, list)
        else set()
    )
    structured = parse_response_format(effective)
    structured_tool_name = (
        structured.name if structured is not None and structured.schema is not None else None
    )
    if structured_tool_name is not None:
        return set(), True, 1, structured_tool_name
    choice = effective.get("tool_choice")
    require_tool_call = choice == "required" or isinstance(choice, dict)
    if choice == "none":
        expected_tool_names = set()
    elif isinstance(choice, dict):
        expected_tool_names = {choice["function"]["name"]}
    max_tool_calls = (
        0 if choice == "none" else 1 if effective.get("parallel_tool_calls") is False else None
    )
    return expected_tool_names, require_tool_call, max_tool_calls, None


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
            expected_tool_names, require_tool_call, max_tool_calls, structured_tool_name = (
                _response_tool_contract(request, model)
            )
            return from_anthropic_response(
                message.model_dump(),
                expected_tool_names=expected_tool_names,
                require_tool_call=require_tool_call,
                max_tool_calls=max_tool_calls,
                structured_tool_name=structured_tool_name,
            )
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
            expected_tool_names, require_tool_call, max_tool_calls, structured_tool_name = (
                _response_tool_contract(request, model)
            )
            return from_anthropic_response(
                message.model_dump(),
                expected_tool_names=expected_tool_names,
                require_tool_call=require_tool_call,
                max_tool_calls=max_tool_calls,
                structured_tool_name=structured_tool_name,
            )
        finally:
            await client.close()

    async def anative_messages(
        self, native_body: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Native passthrough: send the client's Anthropic Messages body upstream
        # verbatim and return the response as-is — NO to_anthropic_request /
        # from_anthropic_response translation. Only the `model` field is resolved
        # from the team alias to the upstream provider id (same as every path),
        # which is not translation. Client lifecycle mirrors achat_completion.
        client = AsyncAnthropic(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )
        body: dict[str, Any] = {**native_body, "model": model.provider_model_id}
        try:
            message: Any = await client.messages.create(**body)
            return message.model_dump()
        finally:
            await client.close()

    async def astream_native_messages(
        self, native_body: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # Native passthrough streaming: relay the upstream Anthropic events verbatim
        # (event.model_dump()) — NO anthropic_event_to_delta, NO OpenAI chunk shape.
        # Only the client's `model` alias is resolved to the provider id (same as
        # every path), which is not translation. Client lifecycle mirrors
        # astream_chat_completion minus the translation + synthetic usage chunk.
        client = AsyncAnthropic(
            api_key=require_api_key(credentials),
            base_url=_base_url(credentials),
            **self._resilience.client_kwargs,
        )
        body: dict[str, Any] = {**native_body, "model": model.provider_model_id, "stream": True}
        # Keep the client open for the whole stream; close on completion/disconnect.
        try:
            stream: Any = await client.messages.create(**body)
            async for event in stream:
                yield event.model_dump()
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
        refused = False
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
                    refused = (raw.get("delta") or {}).get("stop_reason") == "refusal"
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
                    "prompt_tokens": 0 if refused and output_tokens == 0 else input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": (
                        output_tokens
                        if refused and output_tokens == 0
                        else input_tokens + output_tokens
                    ),
                },
            }
        finally:
            await client.close()
