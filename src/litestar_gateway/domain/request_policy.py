"""Deny-by-default sanitizing of a client request before it hits a provider SDK.

The gateway splats the client's OpenAI-shaped body into the SDK call, so without
a policy a tenant could pass SDK-special kwargs (``extra_headers``,
``extra_body``, ``extra_query``, ``timeout`` …) to manipulate how we call the
upstream with *our* credential, or inflate cost with an unbounded ``n`` /
``max_tokens``. This keeps only an explicit allowlist per operation and clamps
the cost-driving numbers. It is a pure function — no I/O.

Only the (untrusted) client request is sanitized; ``model.params`` is trusted
admin/team-admin config and is merged separately by the adapters.
"""

from __future__ import annotations

from typing import Any

from litestar_gateway.domain.chat_tool_policy import (
    MAX_TOOL_COUNT,
    MAX_TOOL_SCHEMA_BYTES,
    json_object,
    serialized_json_size,
    validate_anthropic_tool_name,
)
from litestar_gateway.domain.entities import Model, Provider
from litestar_gateway.domain.exceptions import UnsupportedNativeField, UnsupportedOperation

# Accepted fields per operation. Anything else (including transport overrides
# like extra_headers/extra_body/extra_query/timeout/api_key) is dropped.
_ALLOWED: dict[str, frozenset[str]] = {
    "chat.completions": frozenset(
        {
            "model",
            "messages",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "n",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "response_format",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "seed",
            "stream",
            "stream_options",
            "reasoning_effort",
            "user",
        }
    ),
    "responses": frozenset(
        {
            "background",
            "context_management",
            "conversation",
            "include",
            "model",
            "input",
            "instructions",
            "max_output_tokens",
            "max_tool_calls",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "text",
            "reasoning",
            "metadata",
            "moderation",
            "prompt",
            "prompt_cache_key",
            "prompt_cache_options",
            "prompt_cache_retention",
            "safety_identifier",
            "service_tier",
            "store",
            "previous_response_id",
            "stream",
            "stream_options",
            "top_logprobs",
            "truncation",
            "user",
        }
    ),
    "embeddings": frozenset({"model", "input", "dimensions", "encoding_format", "user"}),
    "images": frozenset(
        {
            "model",
            "prompt",
            "size",
            "quality",
            "style",
            "n",
            "response_format",
            "background",
            "output_format",
            "user",
        }
    ),
}

# Ceilings applied to client-provided values (trusted admin params are not capped).
MAX_N = 8
MAX_TOKENS = 32_000
_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

# Canonical output-token field to inject per operation when a per-model ceiling
# is set but the client sent none. Operations without an output-token concept
# (embeddings, images) are absent and get no injection.
_OUTPUT_TOKEN_FIELD = {
    "chat.completions": "max_tokens",
    "responses": "max_output_tokens",
}

_NATIVE_RESPONSES_PROVIDERS = frozenset({Provider.OPENAI, Provider.AZURE_OPENAI})
_EMULATED_RESPONSES_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "max_output_tokens",
        "temperature",
        "top_p",
        "text",
        "store",
        "stream",
    }
)
_EMULATED_RESPONSES_TOOL_FIELDS = frozenset({"tools", "tool_choice", "parallel_tool_calls"})
_EMULATED_RESPONSES_TOOL_PROVIDERS = frozenset({Provider.DATABRICKS, Provider.ANTHROPIC})
_EMULATED_TEXT_FORMATS = frozenset({"text", "json_object", "json_schema"})
_EMULATED_TEXT_PARTS = frozenset({"text", "input_text", "output_text"})
_EMULATED_MESSAGE_FIELDS = frozenset({"type", "role", "content"})
_EMULATED_MESSAGE_ROLES = frozenset({"user", "assistant", "system"})
_EMULATED_CONTENT_FIELDS = frozenset({"type", "text"})
_EMULATED_FUNCTION_TOOL_FIELDS = frozenset({"type", "name", "description", "parameters", "strict"})
_EMULATED_FUNCTION_CALL_FIELDS = frozenset({"type", "id", "call_id", "name", "arguments", "status"})
_EMULATED_FUNCTION_OUTPUT_FIELDS = frozenset({"type", "id", "call_id", "output", "status"})
_EMULATED_ITEM_STATUSES = frozenset({"completed"})
_EMULATED_TOOL_CHOICES = frozenset({"auto", "none", "required"})
_EMULATED_CHAT_CONFIG_FIELDS_REQUIRING_TOOL_FIDELITY = frozenset(
    {"tools", "tool_choice", "parallel_tool_calls"}
)
_NATIVE_RESPONSES_GOVERNANCE_FIELDS = frozenset(
    {
        "context_management",
        "conversation",
        "previous_response_id",
        "prompt",
        "prompt_cache_retention",
        "service_tier",
    }
)
_NATIVE_RESPONSES_LOCAL_TOOL_TYPES = frozenset({"function", "custom"})


def _clamp_int(value: Any, ceiling: int) -> Any:
    # bool is an int subclass; leave non-ints for the provider to validate.
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return min(value, ceiling)


def sanitize_request(operation: str, request: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `request` with only the allowlisted fields for
    `operation`, cost-driving numbers clamped. Unknown operations pass through."""
    allowed = _ALLOWED.get(operation)
    if allowed is None:  # pragma: no cover - defensive; callers pass known ops
        return dict(request)

    cleaned = {key: value for key, value in request.items() if key in allowed}
    if "n" in cleaned:
        cleaned["n"] = _clamp_int(cleaned["n"], MAX_N)
    for field in _TOKEN_FIELDS:
        if field in cleaned:
            cleaned[field] = _clamp_int(cleaned[field], MAX_TOKENS)
    return cleaned


def _unsupported_responses_fields(provider: Provider, request: dict[str, Any]) -> list[str]:
    allowed = _EMULATED_RESPONSES_FIELDS
    if provider in _EMULATED_RESPONSES_TOOL_PROVIDERS:
        allowed |= _EMULATED_RESPONSES_TOOL_FIELDS
    unsupported = [
        field for field, value in request.items() if field not in allowed and value is not None
    ]
    if request.get("store") not in (None, False):
        unsupported.append("store")
    return sorted(set(unsupported))


def _validate_emulated_text(provider: Provider, text: Any) -> None:
    if text is None:
        return
    if not isinstance(text, dict):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses field 'text' with this shape"
        )
    unsupported = sorted(set(text) - {"format"})
    if unsupported:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses field(s): "
            f"{', '.join(f'text.{field}' for field in unsupported)}"
        )
    fmt = text.get("format")
    if fmt is None:
        return
    format_type = fmt.get("type") if isinstance(fmt, dict) else None
    if (
        not isinstance(fmt, dict)
        or not isinstance(format_type, str)
        or format_type not in _EMULATED_TEXT_FORMATS
    ):
        format_type = fmt.get("type") if isinstance(fmt, dict) else type(fmt).__name__
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses text format '{format_type}'"
        )
    allowed_fields = (
        {"type", "name", "schema", "strict"} if format_type == "json_schema" else {"type"}
    )
    unsupported = sorted(set(fmt) - allowed_fields)
    if unsupported:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses field(s): "
            f"{', '.join(f'text.format.{field}' for field in unsupported)}"
        )


def _unsupported_nested_fields(
    provider: Provider,
    value: dict[str, Any],
    allowed: frozenset[str],
    prefix: str,
) -> None:
    unsupported = sorted(set(value) - allowed)
    if unsupported:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses field(s): "
            f"{', '.join(f'{prefix}.{field}' for field in unsupported)}"
        )


def _validate_item_status(provider: Provider, item: dict[str, Any], prefix: str) -> None:
    status = item.get("status")
    if status is not None and (
        not isinstance(status, str) or status not in _EMULATED_ITEM_STATUSES
    ):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses field "
            f"'{prefix}.status' with value '{status}'"
        )


def _validate_function_call(
    provider: Provider,
    item: dict[str, Any],
    seen_call_ids: set[str],
    declared_tool_names: set[str] | None,
) -> None:
    _unsupported_nested_fields(
        provider,
        item,
        _EMULATED_FUNCTION_CALL_FIELDS,
        "input.function_call",
    )
    call_id = item.get("call_id")
    name = item.get("name")
    arguments = item.get("arguments")
    if not isinstance(call_id, str) or not call_id:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call call_id"
        )
    if call_id in seen_call_ids:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses duplicate call_id"
        )
    if not isinstance(name, str) or not name:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call name"
        )
    if declared_tool_names is not None and name not in declared_tool_names:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot replay a function_call for an undeclared tool name"
        )
    if not isinstance(arguments, str):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call arguments"
        )
    if provider is Provider.ANTHROPIC:
        json_object(
            arguments,
            field="Responses function_call arguments",
            provider=provider,
        )
    item_id = item.get("id")
    if item_id is not None and not isinstance(item_id, str):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call id"
        )
    _validate_item_status(provider, item, "input.function_call")
    seen_call_ids.add(call_id)


def _validate_function_call_output(
    provider: Provider,
    item: dict[str, Any],
    seen_call_ids: set[str],
    completed_call_ids: set[str],
) -> None:
    _unsupported_nested_fields(
        provider,
        item,
        _EMULATED_FUNCTION_OUTPUT_FIELDS,
        "input.function_call_output",
    )
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call_output call_id"
        )
    if call_id not in seen_call_ids:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call_output "
            "without a matching function_call in the stateless input"
        )
    if call_id in completed_call_ids:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses duplicate function_call_output"
        )
    if not isinstance(item.get("output"), str):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call_output output"
        )
    item_id = item.get("id")
    if item_id is not None and not isinstance(item_id, str):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses function_call_output id"
        )
    _validate_item_status(provider, item, "input.function_call_output")
    completed_call_ids.add(call_id)


def _validate_emulated_input(
    provider: Provider,
    value: Any,
    *,
    allow_tools: bool,
    declared_tool_names: set[str] | None = None,
) -> None:
    if value is None or isinstance(value, str):
        return
    if not isinstance(value, list):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses input shape "
            f"'{type(value).__name__}'"
        )
    seen_call_ids: set[str] = set()
    completed_call_ids: set[str] = set()
    pending_call_ids: set[str] = set()
    output_group_started = False
    for item in value:
        if not isinstance(item, dict):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses input item "
                f"'{type(item).__name__}'"
            )
        item_type = item.get("type")
        if item_type == "function_call" and allow_tools:
            if output_group_started and pending_call_ids:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses function_call "
                    "while a prior unresolved function_call has no output"
                )
            if not pending_call_ids:
                output_group_started = False
            _validate_function_call(provider, item, seen_call_ids, declared_tool_names)
            pending_call_ids.add(item["call_id"])
            continue
        if item_type == "function_call_output" and allow_tools:
            _validate_function_call_output(
                provider,
                item,
                seen_call_ids,
                completed_call_ids,
            )
            pending_call_ids.discard(item["call_id"])
            output_group_started = True
            continue
        if pending_call_ids:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses message with an "
                "unresolved function_call"
            )
        output_group_started = False
        if item_type not in (None, "message"):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses input item "
                f"type '{item_type}'"
            )
        unsupported = sorted(set(item) - _EMULATED_MESSAGE_FIELDS)
        if unsupported:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses field(s): "
                f"{', '.join(f'input.{field}' for field in unsupported)}"
            )
        role = item.get("role", "user")
        if not isinstance(role, str) or role not in _EMULATED_MESSAGE_ROLES:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses input role '{role}'"
            )
        _validate_emulated_content(provider, item.get("content"))
    if pending_call_ids:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses input ending with an "
            "unresolved function_call"
        )


def _validate_emulated_tools(provider: Provider, request: dict[str, Any]) -> set[str]:
    tools = request.get("tools")
    tool_names: set[str] = set()
    if tools is not None:
        if not isinstance(tools, list):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses field 'tools' "
                f"with shape '{type(tools).__name__}'"
            )
        if provider is Provider.ANTHROPIC and len(tools) > MAX_TOOL_COUNT:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' accepts at most {MAX_TOOL_COUNT} tools"
            )
        schema_bytes = 0
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses tools[{index}] "
                    f"with shape '{type(tool).__name__}'"
                )
            _unsupported_nested_fields(
                provider,
                tool,
                _EMULATED_FUNCTION_TOOL_FIELDS,
                f"tools[{index}]",
            )
            if tool.get("type") != "function":
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses "
                    f"tools[{index}].type '{tool.get('type')}'"
                )
            if not isinstance(tool.get("name"), str) or not tool["name"]:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses tools[{index}].name"
                )
            name = tool["name"]
            if provider is Provider.ANTHROPIC:
                name = validate_anthropic_tool_name(name, field=f"tools[{index}].name")
            if name in tool_names:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate duplicate tool name '{name}'"
                )
            tool_names.add(name)
            description = tool.get("description")
            if description is not None and not isinstance(description, str):
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses "
                    f"tools[{index}].description"
                )
            parameters = tool.get("parameters")
            if parameters is not None and not isinstance(parameters, dict):
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses "
                    f"tools[{index}].parameters"
                )
            if provider is Provider.ANTHROPIC:
                schema_bytes += serialized_json_size(
                    parameters if parameters is not None else {"type": "object"},
                    field=f"tools[{index}].parameters",
                    provider=provider,
                )
            strict = tool.get("strict")
            if strict is not None and not isinstance(strict, bool):
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses tools[{index}].strict"
                )
        if provider is Provider.ANTHROPIC and schema_bytes > MAX_TOOL_SCHEMA_BYTES:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' accepts at most {MAX_TOOL_SCHEMA_BYTES} bytes "
                "of tool schemas"
            )

    tool_choice = request.get("tool_choice")
    if tool_choice is not None:
        valid_string = isinstance(tool_choice, str) and tool_choice in _EMULATED_TOOL_CHOICES
        valid_named = (
            isinstance(tool_choice, dict)
            and set(tool_choice) == {"type", "name"}
            and tool_choice.get("type") == "function"
            and isinstance(tool_choice.get("name"), str)
            and bool(tool_choice["name"])
        )
        if not valid_string and not valid_named:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses field 'tool_choice' "
                "with this shape"
            )
        if valid_named and tool_choice["name"] not in tool_names:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses tool_choice "
                "for an undefined tool name"
            )

    parallel = request.get("parallel_tool_calls")
    if parallel is not None and not isinstance(parallel, bool):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses field "
            "'parallel_tool_calls' with a non-boolean value"
        )
    return tool_names


def _validate_emulated_content(provider: Provider, content: Any) -> None:
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' cannot emulate Responses input content shape "
            f"'{type(content).__name__}'"
        )
    for part in content:
        if isinstance(part, str):
            continue
        part_type = part.get("type") if isinstance(part, dict) else type(part).__name__
        if (
            not isinstance(part, dict)
            or not isinstance(part_type, str)
            or part_type not in _EMULATED_TEXT_PARTS
            or not isinstance(part.get("text"), str)
        ):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot emulate Responses input content "
                f"type '{part_type}'"
            )
        if isinstance(part, dict):
            unsupported = sorted(set(part) - _EMULATED_CONTENT_FIELDS)
            if unsupported:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot emulate Responses field(s): "
                    f"{', '.join(f'input.content.{field}' for field in unsupported)}"
                )


def _input_resource_paths(request: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    items = request.get("input")
    if not isinstance(items, list):
        return paths
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "item_reference":
            paths.append(f"input[{item_index}].item_reference")
        elif "id" in item and item_type is None:
            paths.append(f"input[{item_index}].id")
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_index, part in enumerate(content):
            part_type = part.get("type") if isinstance(part, dict) else None
            if (
                isinstance(part, dict)
                and isinstance(part_type, str)
                and part_type in {"input_file", "input_image"}
                and part.get("file_id") is not None
            ):
                paths.append(f"input[{item_index}].content[{content_index}].file_id")
    return paths


def _malformed_native_discriminator_paths(request: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    tools = request.get("tools")
    if isinstance(tools, list):
        for index, tool in enumerate(tools):
            if isinstance(tool, dict) and "type" in tool and not isinstance(tool["type"], str):
                paths.append(f"tools[{index}].type")
    items = request.get("input")
    if not isinstance(items, list):
        return paths
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if "type" in item and not isinstance(item["type"], str):
            paths.append(f"input[{item_index}].type")
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_index, part in enumerate(content):
            if isinstance(part, dict) and "type" in part and not isinstance(part["type"], str):
                paths.append(f"input[{item_index}].content[{content_index}].type")
    return paths


def _hosted_tool_paths(request: dict[str, Any]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    paths: list[str] = []
    for index, tool in enumerate(tools):
        tool_type = tool.get("type") if isinstance(tool, dict) else None
        if isinstance(tool_type, str) and tool_type not in _NATIVE_RESPONSES_LOCAL_TOOL_TYPES:
            paths.append(f"tools[{index}].{tool_type}")
    return paths


def _validate_native_responses_governance(model: Model, request: dict[str, Any]) -> dict[str, Any]:
    effective = model.merge_params(request)
    unsupported = [
        field for field in _NATIVE_RESPONSES_GOVERNANCE_FIELDS if effective.get(field) is not None
    ]
    if effective.get("background") not in (None, False):
        unsupported.append("background")
    if effective.get("store") not in (None, False):
        unsupported.append("store")
    unsupported.extend(_malformed_native_discriminator_paths(effective))
    unsupported.extend(_hosted_tool_paths(effective))
    unsupported.extend(_input_resource_paths(effective))
    if unsupported:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' cannot accept Responses field(s) through "
            f"this multi-tenant gateway: {', '.join(sorted(unsupported))}; asynchronous "
            "billing, tier-aware pricing, and tenant-bound provider state are not yet "
            "available"
        )
    return {**request, "store": False}


def _validate_emulated_model_config(model: Model) -> None:
    configured = model.merge_params({})
    unsupported = sorted(
        field
        for field in _EMULATED_CHAT_CONFIG_FIELDS_REQUIRING_TOOL_FIDELITY
        if configured.get(field) is not None
    )
    n = configured.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        unsupported.append("n")
    if configured.get("stream") not in (None, False):
        unsupported.append("stream")
    if unsupported:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' cannot emulate Responses with configured "
            f"model field(s): {', '.join(sorted(set(unsupported)))}"
        )


def validate_responses_request(model: Model, request: dict[str, Any]) -> dict[str, Any]:
    """Return a governed copy, failing when Responses data or policy would be lost."""
    stream = request.get("stream")
    if stream is not None and not isinstance(stream, bool):
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' requires Responses field 'stream' to be boolean"
        )
    if model.provider in _NATIVE_RESPONSES_PROVIDERS:
        return _validate_native_responses_governance(model, request)
    _validate_emulated_model_config(model)
    instructions = request.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' cannot emulate Responses field "
            f"'instructions' with shape '{type(instructions).__name__}'"
        )
    supports_tools = model.provider in _EMULATED_RESPONSES_TOOL_PROVIDERS
    unsupported = _unsupported_responses_fields(model.provider, request)
    if unsupported:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' cannot emulate Responses field(s): "
            f"{', '.join(unsupported)}; use a native Responses provider or remove "
            "the unsupported fields"
        )
    has_tool_request = any(
        request.get(field) is not None for field in _EMULATED_RESPONSES_TOOL_FIELDS
    ) or (
        isinstance(request.get("input"), list)
        and any(
            isinstance(item, dict) and item.get("type") in {"function_call", "function_call_output"}
            for item in request["input"]
        )
    )
    if supports_tools and request.get("stream") is True and has_tool_request:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' cannot emulate Responses streaming tool "
            "calls until the Phase 2 event contract is available"
        )
    tool_names = _validate_emulated_tools(model.provider, request) if supports_tools else set()
    if model.provider is Provider.ANTHROPIC and has_tool_request and not tool_names:
        raise UnsupportedOperation(
            "Provider 'anthropic' requires a non-empty declared tools list for "
            "Responses tool requests"
        )
    _validate_emulated_text(model.provider, request.get("text"))
    text_format = request["text"].get("format") if isinstance(request.get("text"), dict) else None
    if (
        model.provider is Provider.ANTHROPIC
        and has_tool_request
        and (
            (
                isinstance(text_format, dict)
                and text_format.get("type") in {"json_object", "json_schema"}
            )
            or model.merge_params({}).get("response_format") is not None
        )
    ):
        raise UnsupportedOperation(
            "Provider 'anthropic' cannot combine Responses text.format or configured "
            "response_format with client tools"
        )
    _validate_emulated_input(
        model.provider,
        request.get("input"),
        allow_tools=supports_tools,
        declared_tool_names=tool_names if model.provider is Provider.ANTHROPIC else None,
    )
    return dict(request)


# --- Native passthrough governance ---------------------------------------------
#
# The native surfaces forward the client's provider-shaped body verbatim (no
# `sanitize_request`), so the guards the OpenAI surface gets for free must be
# reapplied here on the two governance concerns that touch security/money: the
# reserved SDK control kwargs (credential-override vector) and the output-token
# ceiling. Everything else about the body stays untouched.

# SDK control kwargs the client SDKs treat as transport params, not request
# fields. Splatting them into `messages.create(**body)` lets a tenant override
# the vaulted credential (`extra_headers={"x-api-key": ...}`) or inject outbound
# transport options — so they are rejected on the native surface (leading-
# underscore keys are private SDK params and are rejected too).
_NATIVE_CONTROL_KWARGS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})

# The native output-token field per provider, and how it nests. Anthropic Messages
# carries `max_tokens` top-level; Gemini nests `maxOutputTokens` under
# `generationConfig`. Keyed on the Provider enum so this stays pure request-shape
# policy with no infra dependency.
_NATIVE_OUTPUT_FIELD: dict[Provider, str] = {
    Provider.ANTHROPIC: "max_tokens",
    Provider.VERTEX_AI: "maxOutputTokens",
}


def reject_native_control_kwargs(body: dict[str, Any]) -> None:
    """Reject a native body carrying SDK control kwargs or leading-underscore keys.

    Prefer rejecting over silently stripping so the client learns the field is not
    forwarded (it would otherwise think its override took effect). Provider-agnostic
    — applied to every native request as defense in depth for both surfaces."""
    bad = sorted(k for k in body if k in _NATIVE_CONTROL_KWARGS or k.startswith("_"))
    if bad:
        raise UnsupportedNativeField(f"fields not allowed on the native surface: {bad}")


def _native_effective_ceiling(model_ceiling: int | None) -> int:
    """The output-token ceiling to enforce on a native body: the per-model
    `max_output_tokens` when set, always bounded by the global `MAX_TOKENS`."""
    return min(MAX_TOKENS, model_ceiling) if model_ceiling is not None else MAX_TOKENS


def clamp_native_output_tokens(
    provider: Provider, body: dict[str, Any], model_ceiling: int | None
) -> dict[str, Any]:
    """Enforce the output-token ceiling on a native body's provider-specific field
    (`min(client value, model ceiling, global MAX_TOKENS)`), mirroring the OpenAI
    surface's `clamp_output_tokens`/`sanitize_request`. A present value is clamped
    down; if the client omitted it and a per-model ceiling is set, the field is
    injected at the ceiling so omission cannot bypass the cap. Returns a copy;
    the rest of the body is left verbatim. Unknown providers pass through."""
    field = _NATIVE_OUTPUT_FIELD.get(provider)
    if field is None:  # pragma: no cover - native surfaces are Anthropic/Vertex only
        return body
    ceiling = _native_effective_ceiling(model_ceiling)
    governed = dict(body)
    if provider is Provider.VERTEX_AI:
        config = dict(governed.get("generationConfig") or {})
        value = config.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            config[field] = min(value, ceiling)
            governed["generationConfig"] = config
        elif model_ceiling is not None:
            config[field] = ceiling
            governed["generationConfig"] = config
        return governed
    value = governed.get(field)
    if isinstance(value, int) and not isinstance(value, bool):
        governed[field] = min(value, ceiling)
    elif model_ceiling is not None:
        governed[field] = ceiling
    return governed


def native_reservation_view(provider: Provider, body: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-shaped view of a native body for budget admission + H14 estimation.

    `_reservation_cost`/`_request_text`/`_max_output_tokens` read the OpenAI keys
    (`messages`/`max_tokens`/`n`); the Anthropic Messages body already uses those,
    but a Gemini body carries the prompt under `contents[].parts[].text`, the
    output ceiling under `generationConfig.maxOutputTokens`, and the choice count
    under `generationConfig.candidateCount`. Map the Gemini shape so admission
    reserves the real pessimistic cost — the inverse of `_gemini_usage` at
    settlement. Anthropic passes through unchanged."""
    if provider is not Provider.VERTEX_AI:
        return body
    texts = [
        part["text"]
        for content in body.get("contents") or []
        if isinstance(content, dict)
        for part in content.get("parts") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    config = body.get("generationConfig") or {}
    return {
        "messages": [{"content": "\n".join(texts)}],
        "max_tokens": config.get("maxOutputTokens") or 0,
        "n": config.get("candidateCount") or 1,
    }


def clamp_output_tokens(
    operation: str, request: dict[str, Any], ceiling: int | None
) -> dict[str, Any]:
    """Enforce a per-model output-token `ceiling` with `min` (clamp) semantics.

    Any output-token field the client sent is lowered to `min(value, ceiling)`;
    if the client sent none, the operation's canonical field is injected at the
    ceiling so omission cannot bypass the cap. A `None` ceiling (the default for
    every model) is a no-op — the request passes through unchanged. Returns a
    copy; never mutates the input. Runs after `sanitize_request`, once the model
    is resolved, so the reservation and the provider call see the same numbers."""
    if ceiling is None:
        return request
    cleaned = dict(request)
    present = [field for field in _TOKEN_FIELDS if field in cleaned]
    for field in present:
        cleaned[field] = _clamp_int(cleaned[field], ceiling)
    if not present:
        field = _OUTPUT_TOKEN_FIELD.get(operation)
        if field is not None:
            cleaned[field] = ceiling
    return cleaned
