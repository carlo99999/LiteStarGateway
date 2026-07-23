"""Provider-aware validation for translated OpenAI Chat tool contracts."""

from __future__ import annotations

import json
import re
from typing import Any

from litestar_gateway.domain.entities import Model, Provider
from litestar_gateway.domain.exceptions import UnsupportedOperation

MAX_TOOL_COUNT = 64
MAX_TOOL_SCHEMA_BYTES = 256 * 1024
MAX_TOOL_JSON_DEPTH = 32

_ANTHROPIC_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CHAT_TOOL_CHOICES = frozenset({"auto", "none", "required"})
_TRANSLATED_CHAT_PROVIDERS = frozenset({Provider.ANTHROPIC, Provider.VERTEX_AI, Provider.BEDROCK})
_TEXT_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def json_object(value: Any, *, field: str, provider: Provider) -> dict[str, Any]:
    if not isinstance(value, str):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires {field} to be a JSON object string"
        )
    try:
        decoded = json.loads(value, parse_constant=_reject_non_finite_json)
    except (TypeError, ValueError) as exc:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires {field} to be a valid JSON object"
        ) from exc
    if not isinstance(decoded, dict):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires {field} to decode to a JSON object"
        )
    validate_json_complexity(decoded, field=field, provider=provider)
    return decoded


def validate_json_complexity(value: Any, *, field: str, provider: Provider) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_TOOL_JSON_DEPTH:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot accept {field} deeper than "
                f"{MAX_TOOL_JSON_DEPTH} levels"
            )
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def serialized_json_size(value: Any, *, field: str, provider: Provider) -> int:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires {field} to contain valid JSON"
        ) from exc
    validate_json_complexity(value, field=field, provider=provider)
    return len(encoded)


def validate_anthropic_tool_name(name: Any, *, field: str) -> str:
    if not isinstance(name, str) or _ANTHROPIC_TOOL_NAME.fullmatch(name) is None:
        raise UnsupportedOperation(
            f"Provider '{Provider.ANTHROPIC.value}' requires {field} to match "
            "^[a-zA-Z0-9_-]{1,64}$"
        )
    return name


def _validate_text_content(content: Any, *, provider: Provider, field: str) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise UnsupportedOperation(f"Provider '{provider.value}' requires {field} to contain text")
    for part_index, part in enumerate(content):
        if (
            not isinstance(part, dict)
            or set(part) != {"type", "text"}
            or part.get("type") != "text"
            or not isinstance(part.get("text"), str)
        ):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' does not support non-text or malformed "
                f"{field}[{part_index}] content"
            )


def _validate_messages(effective: dict[str, Any], provider: Provider) -> None:
    messages = effective.get("messages")
    if not isinstance(messages, list):
        raise UnsupportedOperation(f"Provider '{provider.value}' requires messages to be a list")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires messages[{index}] to be an object"
            )
        role = message.get("role")
        if role not in {*_TEXT_MESSAGE_ROLES, "tool"}:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate messages[{index}] role '{role}'"
            )
        if role == "tool":
            unsupported = sorted(set(message) - {"role", "content", "tool_call_id"})
            if unsupported:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot translate "
                    f"messages[{index}].{unsupported[0]}"
                )
            continue
        allowed = {"role", "content", "tool_calls"} if role == "assistant" else {"role", "content"}
        unsupported = sorted(set(message) - allowed)
        if unsupported:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate messages[{index}].{unsupported[0]}"
            )
        if role != "assistant" and message.get("tool_calls") is not None:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate messages[{index}].tool_calls"
            )
        content = message.get("content")
        if content is None and role == "assistant" and message.get("tool_calls") is not None:
            continue
        _validate_text_content(
            content,
            provider=provider,
            field=f"messages[{index}].content",
        )


def _has_tool_contract(request: dict[str, Any]) -> bool:
    if any(
        request.get(field) is not None for field in ("tools", "tool_choice", "parallel_tool_calls")
    ):
        return True
    return any(
        isinstance(message, dict)
        and (message.get("role") in {"tool", "function"} or message.get("tool_calls") is not None)
        for message in request.get("messages") or []
    )


def _validate_tools(effective: dict[str, Any]) -> set[str]:
    provider = Provider.ANTHROPIC
    tools = effective.get("tools")
    if not isinstance(tools, list) or not tools:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires a non-empty tools list for tool calling"
        )
    if len(tools) > MAX_TOOL_COUNT:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' accepts at most {MAX_TOOL_COUNT} tools"
        )
    names: set[str] = set()
    schema_bytes = 0
    for index, tool in enumerate(tools):
        function = tool.get("function") if isinstance(tool, dict) else None
        if (
            not isinstance(tool, dict)
            or set(tool) != {"type", "function"}
            or tool.get("type") != "function"
            or not isinstance(function, dict)
        ):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}] to be an OpenAI function tool"
            )
        unsupported = sorted(set(function) - {"name", "description", "parameters", "strict"})
        if unsupported:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate tools[{index}].{unsupported[0]}"
            )
        name = validate_anthropic_tool_name(
            function.get("name"), field=f"tools[{index}].function.name"
        )
        if name in names:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate duplicate tool name '{name}'"
            )
        names.add(name)
        description = function.get("description")
        if description is not None and not isinstance(description, str):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}].function.description "
                "to be a string"
            )
        parameters = function.get("parameters", {"type": "object"})
        if not isinstance(parameters, dict):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}].function.parameters "
                "to be a JSON object"
            )
        schema_bytes += serialized_json_size(
            parameters,
            field=f"tools[{index}].function.parameters",
            provider=provider,
        )
        strict = function.get("strict")
        if strict is not None and not isinstance(strict, bool):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}].function.strict to be boolean"
            )
    if schema_bytes > MAX_TOOL_SCHEMA_BYTES:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' accepts at most {MAX_TOOL_SCHEMA_BYTES} bytes "
            "of tool schemas"
        )
    return names


def _validate_tool_choice(effective: dict[str, Any], tool_names: set[str]) -> None:
    provider = Provider.ANTHROPIC
    choice = effective.get("tool_choice")
    if choice is not None:
        valid_string = isinstance(choice, str) and choice in _CHAT_TOOL_CHOICES
        function = choice.get("function") if isinstance(choice, dict) else None
        valid_named = (
            isinstance(choice, dict)
            and set(choice) == {"type", "function"}
            and choice.get("type") == "function"
            and isinstance(function, dict)
            and set(function) == {"name"}
            and function.get("name") in tool_names
        )
        if not valid_string and not valid_named:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate tool_choice with this shape "
                "or undefined tool name"
            )
    parallel = effective.get("parallel_tool_calls")
    if parallel is not None and not isinstance(parallel, bool):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires parallel_tool_calls to be boolean"
        )


def _validate_tool_replay(effective: dict[str, Any], tool_names: set[str]) -> None:
    provider = Provider.ANTHROPIC
    seen_call_ids: set[str] = set()
    pending_call_ids: set[str] = set()
    for index, message in enumerate(effective.get("messages") or []):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls is not None:
            if pending_call_ids or not isinstance(tool_calls, list) or not tool_calls:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot translate messages[{index}].tool_calls"
                )
            for call_index, call in enumerate(tool_calls):
                function = call.get("function") if isinstance(call, dict) else None
                call_id = call.get("id") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                arguments = function.get("arguments") if isinstance(function, dict) else None
                if (
                    not isinstance(call, dict)
                    or set(call) != {"id", "type", "function"}
                    or call.get("type") != "function"
                    or not isinstance(call_id, str)
                    or not call_id
                    or call_id in seen_call_ids
                    or not isinstance(function, dict)
                    or set(function) != {"name", "arguments"}
                    or name not in tool_names
                ):
                    raise UnsupportedOperation(
                        f"Provider '{provider.value}' cannot translate "
                        f"messages[{index}].tool_calls[{call_index}]"
                    )
                json_object(
                    arguments,
                    field=f"messages[{index}].tool_calls[{call_index}].function.arguments",
                    provider=provider,
                )
                seen_call_ids.add(call_id)
                pending_call_ids.add(call_id)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if (
                not pending_call_ids
                or not isinstance(call_id, str)
                or call_id not in pending_call_ids
                or not isinstance(message.get("content"), str)
            ):
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot translate messages[{index}] tool result"
                )
            pending_call_ids.remove(call_id)
            continue
        if pending_call_ids:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate messages[{index}] while "
                "prior tool calls have no result"
            )
    if pending_call_ids:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot translate an incomplete tool replay"
        )


def validate_chat_request(model: Model, request: dict[str, Any]) -> dict[str, Any]:
    """Fail before routing/admission when a translated provider would lose Chat data."""
    raw_stream = request.get("stream")
    if raw_stream is not None and not isinstance(raw_stream, bool):
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' requires Chat field 'stream' to be boolean"
        )
    if model.provider not in _TRANSLATED_CHAT_PROVIDERS:
        return dict(request)
    effective = model.merge_params(request)
    effective_stream = effective.get("stream")
    if effective_stream is not None and not isinstance(effective_stream, bool):
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' requires Chat field 'stream' to be boolean"
        )
    _validate_messages(effective, model.provider)
    if not _has_tool_contract(effective):
        return dict(request)
    if model.provider is not Provider.ANTHROPIC:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' does not support tool/function calling; "
            "route this request to an OpenAI, Azure, Databricks, or Anthropic model."
        )
    if raw_stream is True or effective_stream is True:
        raise UnsupportedOperation(
            "Provider 'anthropic' does not support streaming tool calls until the "
            "Phase 2 event contract is available"
        )
    if effective.get("response_format") is not None:
        raise UnsupportedOperation(
            "Provider 'anthropic' cannot combine response_format with client tools"
        )
    tool_names = _validate_tools(effective)
    _validate_tool_choice(effective, tool_names)
    _validate_tool_replay(effective, tool_names)
    return dict(request)
