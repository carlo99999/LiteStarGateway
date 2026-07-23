"""Emulate the Responses API on top of chat.completions.

For providers without a native Responses API (e.g. Databricks), we translate a
Responses request into a chat.completions call and reshape the chat response
into a Responses object. Pure translators + a thin wrapper adapter.

The client keeps using the stock OpenAI SDK (`client.responses.create`), so it
"just works" — text-in/text-out plus structured outputs (`text.format` is
translated to the chat `response_format`, which each adapter maps per provider).
Non-streaming function calls are also translated when the wrapped chat adapter
supports the OpenAI Chat tool contract (currently Databricks). The
provider-aware request policy rejects these before budget admission and
dispatch when unsupported:
  * streaming tools / function calling
  * multimodal input (images, files, audio)
  * stateful conversations (`previous_response_id`, `store`)
  * reasoning items / built-in tools (web search, file search, ...)
Natively-supported providers (OpenAI, Azure) receive the governed synchronous,
stateless native SDK request surface unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import UpstreamResponseInvalid

# Chat sampling params we carry over verbatim from a Responses request.
_PASSTHROUGH = frozenset(
    {"temperature", "top_p", "stop", "presence_penalty", "frequency_penalty", "seed", "n"}
)
_USAGE_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            c["text"] if isinstance(c, dict) and "text" in c else c
            for c in content
            if isinstance(c, dict | str)
        ]
        return "".join(p for p in parts if isinstance(p, str))
    return ""


def _chat_tool_call(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["call_id"],
        "type": "function",
        "function": {
            "name": item["name"],
            "arguments": item["arguments"],
        },
    }


def _input_to_messages(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    messages: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    def flush_tool_calls() -> None:
        nonlocal pending_tool_calls
        if not pending_tool_calls:
            return
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": pending_tool_calls,
            }
        )
        pending_tool_calls = []

    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            pending_tool_calls.append(_chat_tool_call(item))
            continue
        flush_tool_calls()
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": item["output"],
                }
            )
            continue
        messages.append(
            {"role": item.get("role", "user"), "content": _extract_text(item.get("content"))}
        )
    flush_tool_calls()
    return messages


def _to_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    for tool in tools:
        function = {
            key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool
        }
        translated.append({"type": "function", "function": function})
    return translated


def _to_chat_tool_choice(choice: Any) -> Any:
    if isinstance(choice, dict):
        return {
            "type": "function",
            "function": {"name": choice["name"]},
        }
    return choice


def to_chat_completions(request: dict[str, Any]) -> dict[str, Any]:
    """Responses-shaped request -> chat.completions-shaped request."""
    messages: list[dict[str, Any]] = []
    if instructions := request.get("instructions"):
        messages.append({"role": "system", "content": instructions})
    messages.extend(_input_to_messages(request.get("input")))

    chat: dict[str, Any] = {"messages": messages}
    for key in _PASSTHROUGH:
        if key in request:
            chat[key] = request[key]
    if "max_output_tokens" in request:
        chat["max_tokens"] = request["max_output_tokens"]
    if response_format := _text_format_to_response_format(request.get("text")):
        chat["response_format"] = response_format
    if isinstance(request.get("tools"), list):
        chat["tools"] = _to_chat_tools(request["tools"])
    if "tool_choice" in request:
        chat["tool_choice"] = _to_chat_tool_choice(request["tool_choice"])
    if "parallel_tool_calls" in request:
        chat["parallel_tool_calls"] = request["parallel_tool_calls"]
    return chat


def _text_format_to_response_format(text: Any) -> dict[str, Any] | None:
    """Map a Responses `text.format` block to a chat `response_format`, so the
    inner chat adapter applies the same cross-provider structured-output logic.

    Responses puts the schema fields flat under `format`; chat nests them under
    `json_schema`."""
    fmt = text.get("format") if isinstance(text, dict) else None
    if not isinstance(fmt, dict):
        return None
    ftype = fmt.get("type")
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {k: fmt[k] for k in ("name", "schema", "strict") if k in fmt},
        }
    return None


def to_responses(chat_response: dict[str, Any]) -> dict[str, Any]:
    """chat.completions response -> Responses-shaped response."""
    choices = chat_response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _invalid_response(chat_response, "upstream provider returned a malformed response")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _invalid_response(chat_response, "upstream provider returned a malformed response")
    content = message.get("content")
    text = "" if content is None else content
    raw_tool_calls = message.get("tool_calls")
    tool_calls = [] if raw_tool_calls is None else raw_tool_calls
    if not isinstance(text, str) or not isinstance(tool_calls, list):
        raise _invalid_response(
            chat_response,
            "upstream provider returned a malformed tool call response",
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason not in {"stop", "tool_calls"}:
        raise _invalid_response(
            chat_response,
            "upstream provider returned an incomplete response",
        )
    if bool(tool_calls) != (finish_reason == "tool_calls"):
        raise _invalid_response(
            chat_response,
            "upstream provider returned a malformed tool call response",
        )
    output: list[dict[str, Any]] = []
    if text or not tool_calls:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    output.extend(_function_call_items(tool_calls, chat_response))
    usage = _validated_usage(chat_response)
    return {
        "id": chat_response.get("id"),
        "object": "response",
        "created_at": chat_response.get("created"),
        "model": chat_response.get("model"),
        "status": "completed",
        "output": output,
        "output_text": text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
    }


def _invalid_response(
    chat_response: dict[str, Any],
    message: str,
) -> UpstreamResponseInvalid:
    return UpstreamResponseInvalid(message, _billable_response(chat_response))


def _billable_response(chat_response: dict[str, Any]) -> dict[str, Any]:
    raw_usage = chat_response.get("usage")
    if not isinstance(raw_usage, dict):
        return {"usage": {}}
    usage = {
        field: value
        for field in _USAGE_TOKEN_FIELDS
        if isinstance((value := raw_usage.get(field)), int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return {"usage": usage}


def _validated_usage(chat_response: dict[str, Any]) -> dict[str, Any]:
    raw_usage = chat_response.get("usage")
    if raw_usage is None:
        return {}
    if not isinstance(raw_usage, dict):
        raise _invalid_response(
            chat_response,
            "upstream provider returned malformed usage",
        )
    for field in _USAGE_TOKEN_FIELDS:
        value = raw_usage.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise _invalid_response(
                chat_response,
                "upstream provider returned malformed usage",
            )
    return raw_usage


def _function_call_items(
    tool_calls: list[Any],
    chat_response: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    for tool_call in tool_calls:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        call_type = tool_call.get("type") if isinstance(tool_call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if (
            call_type != "function"
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
        ):
            raise _invalid_response(
                chat_response,
                "upstream provider returned a malformed tool call",
            )
        if call_id in seen_call_ids:
            raise _invalid_response(
                chat_response,
                "upstream provider returned a duplicate tool call id",
            )
        seen_call_ids.add(call_id)
        items.append(
            {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return items


class ChatToResponsesAdapter:
    """Wraps a chat-only adapter to also serve the Responses API via emulation."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._inner.chat_completion(request, model, credentials)

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._inner.achat_completion(request, model, credentials)

    def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        return self._inner.astream_chat_completion(request, model, credentials)

    async def astream_responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        """Emulate Responses streaming over the chat stream: created → text
        deltas → completed. A subset of the native event protocol."""
        chat_request = to_chat_completions(request)
        response_id = "resp-emulated"
        meta = {"id": response_id, "object": "response", "model": model.provider_model_id}
        yield {
            "type": "response.created",
            "response": {**meta, "status": "in_progress", "output": []},
        }

        parts: list[str] = []
        usage: dict[str, Any] = {}
        async for chunk in self._inner.astream_chat_completion(chat_request, model, credentials):
            # Inner chat streams end with a usage-bearing chunk (adapters force
            # it); carry it into response.completed so the call can be metered.
            if chunk.get("usage"):
                usage = chunk["usage"]
            content = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
            if content:
                parts.append(content)
                yield {"type": "response.output_text.delta", "delta": content}

        text = "".join(parts)
        yield {
            "type": "response.completed",
            "response": {
                **meta,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "output_text": text,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                },
            },
        }

    async def anative_messages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Native passthrough is provider-specific and untranslated; forward to the
        # wrapped adapter (only Anthropic reaches here — the native surface guards
        # provider before dispatch).
        return await self._inner.anative_messages(request, model, credentials)

    def astream_native_messages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # Native streaming passthrough: forward the raw Anthropic event stream from
        # the wrapped adapter unchanged (mirrors astream_chat_completion — returns
        # the inner async iterator directly, no translation).
        return self._inner.astream_native_messages(request, model, credentials)

    async def agenerate_content(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Native Gemini passthrough is provider-specific and untranslated; forward
        # to the wrapped adapter (only Vertex reaches here — the native surface
        # guards provider before dispatch).
        return await self._inner.agenerate_content(request, model, credentials)

    def astream_generate_content(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # Native Gemini streaming passthrough: forward the raw chunk stream from the
        # wrapped adapter unchanged (mirrors astream_chat_completion — returns the
        # inner async iterator directly, no translation).
        return self._inner.astream_generate_content(request, model, credentials)

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._inner.embeddings(request, model, credentials)

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._inner.aembeddings(request, model, credentials)

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return self._inner.images(request, model, credentials)

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await self._inner.aimages(request, model, credentials)

    def responses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        chat = self._inner.chat_completion(to_chat_completions(request), model, credentials)
        return to_responses(chat)

    async def aresponses(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        chat = await self._inner.achat_completion(to_chat_completions(request), model, credentials)
        return to_responses(chat)
