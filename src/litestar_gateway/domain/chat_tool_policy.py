"""Provider-aware validation for translated OpenAI Chat tool contracts."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from litestar_gateway.domain.entities import Model, Provider
from litestar_gateway.domain.exceptions import UnsupportedOperation

MAX_TOOL_COUNT = 64
MAX_TOOL_SCHEMA_BYTES = 256 * 1024
MAX_TOOL_JSON_DEPTH = 32
MAX_VERTEX_THOUGHT_SIGNATURE_BYTES = 64 * 1024
VERTEX_THOUGHT_SIGNATURE_BYPASS = b"skip_thought_signature_validator"
VERTEX_GATEWAY_CALL_ID_PREFIX = "function-call-gateway-"

_ANTHROPIC_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_VERTEX_FUNCTION_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.:-]{0,127}$")
_VERTEX_PARAMETER_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_BEDROCK_TOOL_USE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,64}$")
_BEDROCK_TOOL_MODEL_ID = re.compile(
    r"^(?:(?:us|eu|apac|global)\.)?"
    r"(?:anthropic\.claude-3(?:[-.:]|$)|amazon\.nova-)"
)
_BEDROCK_NOVA_MODEL_ID = re.compile(r"^(?:(?:us|eu|apac|global)\.)?amazon\.nova-")
_VERTEX_TOOL_MODEL_ID = re.compile(
    r"^gemini-(?:"
    r"2\.5-(?:pro|flash(?:-lite)?)|"
    r"3-(?:pro|flash)|"
    r"3\.1-(?:pro|flash-lite)|"
    r"3\.5-flash(?:-lite)?|"
    r"3\.6-flash"
    r")(?:-(?:preview(?:-\d{2}-\d{2})?|\d{3}|latest))?$"
)
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


def validate_tool_name(name: Any, *, field: str, provider: Provider) -> str:
    if not isinstance(name, str) or _ANTHROPIC_TOOL_NAME.fullmatch(name) is None:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires {field} to match ^[a-zA-Z0-9_-]{{1,64}}$"
        )
    return name


def validate_anthropic_tool_name(name: Any, *, field: str) -> str:
    """Backward-compatible Anthropic-specific wrapper used by Responses policy."""
    return validate_tool_name(name, field=field, provider=Provider.ANTHROPIC)


def validate_vertex_tool_name(name: Any, *, field: str) -> str:
    if not isinstance(name, str) or _VERTEX_FUNCTION_NAME.fullmatch(name) is None:
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field} to match ^[a-zA-Z_][a-zA-Z0-9_.:-]{{0,127}}$"
        )
    return name


def validate_bedrock_tool_use_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _BEDROCK_TOOL_USE_ID.fullmatch(value) is None:
        raise UnsupportedOperation(
            f"Provider '{Provider.BEDROCK.value}' requires {field} to match "
            "^[a-zA-Z0-9_.:-]{1,64}$"
        )
    return value


def decode_vertex_thought_signature(value: Any, *, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field} to be a non-empty base64 string"
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field} to be canonical base64"
        ) from exc
    if (
        not decoded
        or decoded == VERTEX_THOUGHT_SIGNATURE_BYPASS
        or len(decoded) > MAX_VERTEX_THOUGHT_SIGNATURE_BYTES
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field} to be canonical base64 no larger than "
            f"{MAX_VERTEX_THOUGHT_SIGNATURE_BYTES} decoded bytes"
        )
    return decoded


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
            unsupported = sorted(
                field
                for field in set(message) - {"role", "content", "tool_call_id"}
                if message[field] is not None
            )
            if unsupported:
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot translate "
                    f"messages[{index}].{unsupported[0]}"
                )
            continue
        allowed = {"role", "content", "tool_calls"} if role == "assistant" else {"role", "content"}
        unsupported = sorted(
            field for field in set(message) - allowed if message[field] is not None
        )
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


def validate_bedrock_tool_schema(model: Model, schema: Any, *, field: str) -> None:
    if not isinstance(schema, dict):
        raise UnsupportedOperation(f"Provider 'bedrock' requires {field} to be a JSON object")
    if _BEDROCK_NOVA_MODEL_ID.match(model.provider_model_id.lower()) is None:
        return
    unsupported = sorted(set(schema) - {"type", "properties", "required"})
    if (
        schema.get("type") != "object"
        or unsupported
        or ("properties" in schema and not isinstance(schema["properties"], dict))
        or (
            "required" in schema
            and (
                not isinstance(schema["required"], list)
                or any(not isinstance(name, str) for name in schema["required"])
            )
        )
    ):
        raise UnsupportedOperation(
            "Provider 'bedrock' Amazon Nova tool schemas require a top-level "
            "object containing only type, properties, and required"
        )


_VERTEX_SCHEMA_FIELDS = frozenset(
    {
        "type",
        "nullable",
        "required",
        "format",
        "description",
        "properties",
        "items",
        "enum",
        "anyOf",
    }
)
_VERTEX_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


def _validate_vertex_schema_node(schema: Any, *, field: str) -> None:
    if not isinstance(schema, dict):
        raise UnsupportedOperation(f"Provider 'vertex_ai' requires {field} to be an object")
    unsupported = sorted(set(schema) - _VERTEX_SCHEMA_FIELDS)
    if unsupported:
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' cannot translate {field}.{unsupported[0]}"
        )
    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or schema_type not in _VERTEX_SCHEMA_TYPES
    ):
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field}.type to be a supported JSON Schema type"
        )
    nullable = schema.get("nullable")
    if nullable is not None and not isinstance(nullable, bool):
        raise UnsupportedOperation(f"Provider 'vertex_ai' requires {field}.nullable to be boolean")
    for string_field in ("format", "description"):
        value = schema.get(string_field)
        if value is not None and not isinstance(value, str):
            raise UnsupportedOperation(
                f"Provider 'vertex_ai' requires {field}.{string_field} to be a string"
            )
    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, list) or not enum or any(not isinstance(value, str) for value in enum)
    ):
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field}.enum to be a non-empty list of strings"
        )
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise UnsupportedOperation(
                f"Provider 'vertex_ai' requires {field}.properties to be an object"
            )
        for name, child in properties.items():
            if not isinstance(name, str) or _VERTEX_PARAMETER_NAME.fullmatch(name) is None:
                raise UnsupportedOperation(
                    f"Provider 'vertex_ai' requires {field}.properties names to match "
                    "^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"
                )
            _validate_vertex_schema_node(child, field=f"{field}.properties.{name}")
    items = schema.get("items")
    if items is not None:
        _validate_vertex_schema_node(items, field=f"{field}.items")
    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise UnsupportedOperation(
                f"Provider 'vertex_ai' requires {field}.anyOf to be a non-empty list"
            )
        for index, child in enumerate(any_of):
            _validate_vertex_schema_node(child, field=f"{field}.anyOf[{index}]")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(
            not isinstance(name, str) or _VERTEX_PARAMETER_NAME.fullmatch(name) is None
            for name in required
        )
        or len(required) != len(set(required))
    ):
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field}.required to contain unique "
            "Vertex parameter names"
        )


def validate_vertex_tool_schema(schema: Any, *, field: str) -> None:
    _validate_vertex_schema_node(schema, field=field)
    if schema.get("type") != "object":
        raise UnsupportedOperation(f"Provider 'vertex_ai' requires {field}.type to be 'object'")


def vertex_supports_tools(model_id: str) -> bool:
    normalized = model_id.lower()
    if any(marker in normalized for marker in ("embedding", "image", "live", "tts")):
        return False
    return _VERTEX_TOOL_MODEL_ID.match(normalized) is not None


def validate_bedrock_tool_strict(strict: Any, *, field: str) -> None:
    if strict is not None and not isinstance(strict, bool):
        raise UnsupportedOperation(f"Provider 'bedrock' requires {field} to be boolean")
    if strict is True:
        raise UnsupportedOperation(
            "Provider 'bedrock' strict tool schemas are not enabled for the "
            "validated Claude 3 and Nova model matrix"
        )


def _validate_tools(effective: dict[str, Any], model: Model) -> set[str]:
    provider = model.provider
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
        if provider is Provider.VERTEX_AI:
            name = validate_vertex_tool_name(
                function.get("name"),
                field=f"tools[{index}].function.name",
            )
        else:
            name = validate_tool_name(
                function.get("name"),
                field=f"tools[{index}].function.name",
                provider=provider,
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
        if provider is Provider.BEDROCK and description == "":
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}].function.description "
                "to be non-empty when provided"
            )
        parameters = function.get("parameters", {"type": "object"})
        if not isinstance(parameters, dict):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}].function.parameters "
                "to be a JSON object"
            )
        validate_json_complexity(
            parameters,
            field=f"tools[{index}].function.parameters",
            provider=provider,
        )
        if provider is Provider.BEDROCK:
            validate_bedrock_tool_schema(
                model,
                parameters,
                field=f"tools[{index}].function.parameters",
            )
        elif provider is Provider.VERTEX_AI:
            validate_vertex_tool_schema(
                parameters,
                field=f"tools[{index}].function.parameters",
            )
        schema_bytes += serialized_json_size(
            parameters,
            field=f"tools[{index}].function.parameters",
            provider=provider,
        )
        strict = function.get("strict")
        if provider is Provider.BEDROCK:
            validate_bedrock_tool_strict(
                strict,
                field=f"tools[{index}].function.strict",
            )
        elif provider is Provider.VERTEX_AI and strict is not None:
            raise UnsupportedOperation(
                f"Provider '{provider.value}' cannot translate tools[{index}].function.strict"
            )
        elif strict is not None and not isinstance(strict, bool):
            raise UnsupportedOperation(
                f"Provider '{provider.value}' requires tools[{index}].function.strict to be boolean"
            )
    if schema_bytes > MAX_TOOL_SCHEMA_BYTES:
        raise UnsupportedOperation(
            f"Provider '{provider.value}' accepts at most {MAX_TOOL_SCHEMA_BYTES} bytes "
            "of tool schemas"
        )
    return names


def bedrock_supports_named_tool_choice(model_id: str) -> bool:
    return _BEDROCK_TOOL_MODEL_ID.match(model_id.lower()) is not None


def bedrock_supports_tools(model_id: str) -> bool:
    # Keep the translated contract conservative: these are the two families for
    # which AWS documents the full Converse choice surface. Opaque ARNs can
    # point at profiles whose backing family is not recoverable from the ID.
    return bedrock_supports_named_tool_choice(model_id)


def validate_bedrock_structured_tool(
    model: Model,
    *,
    name: Any,
    schema: Any,
    field: str,
) -> None:
    if not bedrock_supports_tools(model.provider_model_id):
        raise UnsupportedOperation(
            "Provider 'bedrock' can evaluate structured JSON schema capability "
            "only for validated Anthropic Claude 3 and Amazon Nova model IDs"
        )
    validate_tool_name(name, field=f"{field}.name", provider=Provider.BEDROCK)
    if not isinstance(schema, dict):
        raise UnsupportedOperation(
            f"Provider 'bedrock' requires {field}.schema to be a JSON object"
        )
    size = serialized_json_size(
        schema,
        field=f"{field}.schema",
        provider=Provider.BEDROCK,
    )
    if size > MAX_TOOL_SCHEMA_BYTES:
        raise UnsupportedOperation(
            f"Provider 'bedrock' accepts at most {MAX_TOOL_SCHEMA_BYTES} bytes "
            "for a structured-output schema"
        )
    raise UnsupportedOperation(
        "Provider 'bedrock' json_schema structured output is not enabled for the "
        "validated Claude 3 and Nova model matrix"
    )


def validate_bedrock_response_format(model: Model, effective: dict[str, Any]) -> None:
    response_format = effective.get("response_format")
    if model.provider is not Provider.BEDROCK or response_format is None:
        return
    if not isinstance(response_format, dict):
        raise UnsupportedOperation("Provider 'bedrock' requires response_format to be an object")
    format_type = response_format.get("type")
    if format_type in {"text", "json_object"}:
        if set(response_format) != {"type"}:
            unsupported = next(iter(set(response_format) - {"type"}))
            raise UnsupportedOperation(
                f"Provider 'bedrock' cannot translate response_format.{unsupported}"
            )
        return
    if format_type != "json_schema":
        raise UnsupportedOperation(
            f"Provider 'bedrock' cannot translate response_format type '{format_type}'"
        )
    if set(response_format) != {"type", "json_schema"}:
        raise UnsupportedOperation("Provider 'bedrock' requires response_format json_schema shape")
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, dict):
        raise UnsupportedOperation(
            "Provider 'bedrock' requires response_format.json_schema to be an object"
        )
    unsupported = sorted(set(json_schema) - {"name", "description", "schema", "strict"})
    if unsupported:
        raise UnsupportedOperation(
            f"Provider 'bedrock' cannot translate response_format.json_schema.{unsupported[0]}"
        )
    description = json_schema.get("description")
    if description is not None and not isinstance(description, str):
        raise UnsupportedOperation(
            "Provider 'bedrock' requires response_format.json_schema.description to be a string"
        )
    strict = json_schema.get("strict")
    if strict is not None and not isinstance(strict, bool):
        raise UnsupportedOperation(
            "Provider 'bedrock' requires response_format.json_schema.strict to be boolean"
        )
    validate_bedrock_structured_tool(
        model,
        name=json_schema.get("name") or "structured_output",
        schema=json_schema.get("schema"),
        field="response_format.json_schema",
    )


def _validate_tool_choice(
    effective: dict[str, Any],
    tool_names: set[str],
    model: Model,
) -> None:
    provider = model.provider
    choice = effective.get("tool_choice")
    valid_named = False
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
    if provider is Provider.BEDROCK:
        if choice == "none":
            raise UnsupportedOperation(
                "Provider 'bedrock' cannot translate tool_choice='none'; "
                "Converse has no disabled-tools choice"
            )
        if valid_named and not bedrock_supports_named_tool_choice(model.provider_model_id):
            raise UnsupportedOperation(
                "Provider 'bedrock' supports a named tool_choice only for "
                "Anthropic Claude 3 and Amazon Nova models"
            )
    parallel = effective.get("parallel_tool_calls")
    if parallel is not None and not isinstance(parallel, bool):
        raise UnsupportedOperation(
            f"Provider '{provider.value}' requires parallel_tool_calls to be boolean"
        )
    if provider is Provider.BEDROCK and parallel is False:
        raise UnsupportedOperation(
            "Provider 'bedrock' cannot translate parallel_tool_calls=false; "
            "Converse has no general disabled-parallel setting"
        )
    if provider is Provider.VERTEX_AI and parallel is False:
        raise UnsupportedOperation(
            "Provider 'vertex_ai' cannot translate parallel_tool_calls=false; "
            "Gemini has no general disabled-parallel setting"
        )


def _validate_vertex_extra_content(
    extra_content: Any,
    *,
    call_id: str,
    field: str,
) -> bool:
    if extra_content is None:
        return False
    if (
        not isinstance(extra_content, dict)
        or not extra_content
        or set(extra_content) - {"google", "litestar_gateway"}
    ):
        raise UnsupportedOperation(
            f"Provider 'vertex_ai' requires {field} to contain only documented metadata"
        )
    google = extra_content.get("google")
    if "google" in extra_content:
        if not isinstance(google, dict) or set(google) != {"thought_signature"}:
            raise UnsupportedOperation(
                f"Provider 'vertex_ai' requires {field}.google to contain only thought_signature"
            )
        decode_vertex_thought_signature(
            google["thought_signature"],
            field=f"{field}.google.thought_signature",
        )
    gateway = extra_content.get("litestar_gateway")
    if "litestar_gateway" in extra_content:
        if (
            not isinstance(gateway, dict)
            or set(gateway) != {"synthetic_call_id"}
            or gateway.get("synthetic_call_id") is not True
            or not call_id.startswith(VERTEX_GATEWAY_CALL_ID_PREFIX)
        ):
            raise UnsupportedOperation(
                f"Provider 'vertex_ai' requires {field}.litestar_gateway to mark only "
                "gateway-generated call IDs"
            )
    return "google" in extra_content


def _validate_tool_replay(
    effective: dict[str, Any],
    tool_names: set[str],
    model: Model,
) -> None:
    provider = model.provider
    seen_call_ids: set[str] = set()
    pending_call_ids: set[str] = set()
    pending_call_order: list[str] = []
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
                allowed_call_fields = {"id", "type", "function"}
                if provider is Provider.VERTEX_AI:
                    allowed_call_fields.add("extra_content")
                if (
                    not isinstance(call, dict)
                    or set(call) - allowed_call_fields
                    or not {"id", "type", "function"}.issubset(call)
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
                if provider is Provider.BEDROCK:
                    validate_bedrock_tool_use_id(
                        call_id,
                        field=f"messages[{index}].tool_calls[{call_index}].id",
                    )
                json_object(
                    arguments,
                    field=f"messages[{index}].tool_calls[{call_index}].function.arguments",
                    provider=provider,
                )
                seen_call_ids.add(call_id)
                pending_call_ids.add(call_id)
                pending_call_order.append(call_id)
                if provider is Provider.VERTEX_AI:
                    has_signature = _validate_vertex_extra_content(
                        call.get("extra_content"),
                        call_id=call_id,
                        field=f"messages[{index}].tool_calls[{call_index}].extra_content",
                    )
                    if has_signature and call_index != 0:
                        raise UnsupportedOperation(
                            "Provider 'vertex_ai' requires thought_signature only on "
                            "the first function call in a parallel group"
                        )
            if (
                provider is Provider.VERTEX_AI
                and model.provider_model_id.lower().startswith("gemini-3")
                and isinstance(tool_calls[0], dict)
                and not (
                    isinstance(tool_calls[0].get("extra_content"), dict)
                    and isinstance(tool_calls[0]["extra_content"].get("google"), dict)
                )
            ):
                raise UnsupportedOperation(
                    "Provider 'vertex_ai' requires thought_signature on the first "
                    "replayed Gemini 3 function call"
                )
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if (
                not pending_call_ids
                or not isinstance(call_id, str)
                or call_id not in pending_call_ids
                or not isinstance(message.get("content"), str)
                or (
                    provider is Provider.VERTEX_AI
                    and pending_call_order
                    and call_id != pending_call_order[0]
                )
            ):
                raise UnsupportedOperation(
                    f"Provider '{provider.value}' cannot translate messages[{index}] tool result"
                )
            pending_call_ids.remove(call_id)
            pending_call_order.remove(call_id)
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
    validate_bedrock_response_format(model, effective)
    if not _has_tool_contract(effective):
        return dict(request)
    if model.provider is Provider.VERTEX_AI and not vertex_supports_tools(model.provider_model_id):
        raise UnsupportedOperation(
            "Provider 'vertex_ai' tool calling is enabled only for validated "
            "Gemini 2.5 and Gemini 3 text model IDs"
        )
    if model.provider is Provider.BEDROCK and not bedrock_supports_tools(model.provider_model_id):
        raise UnsupportedOperation(
            "Provider 'bedrock' tool calling is enabled only for validated "
            "Anthropic Claude 3 and Amazon Nova model IDs"
        )
    if raw_stream is True or effective_stream is True:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' does not support streaming tool calls until the "
            "Phase 2 event contract is available"
        )
    if effective.get("response_format") is not None:
        raise UnsupportedOperation(
            f"Provider '{model.provider.value}' cannot combine response_format with client tools"
        )
    tool_names = _validate_tools(effective, model)
    _validate_tool_choice(effective, tool_names, model)
    _validate_tool_replay(effective, tool_names, model)
    return dict(request)
