"""Vertex AI (Gemini) adapter: translates OpenAI chat.completions ↔ Gemini.

Pure translators (`to_gemini_request` / `from_gemini_response`) + a thin client
wrapper using the `google-genai` SDK. Responses are provided by wrapping this
adapter in `ChatToResponsesAdapter`.

Credential `values`: `vertex_project`, `vertex_location`, and (in production)
`vertex_credentials` — the service-account JSON. Without it, Application Default
Credentials are used.

Scope: text-in/text-out, structured outputs (`response_format`) translated to
`response_mime_type` + `response_schema`, and faithful non-streaming function
tools with thought-signature replay. Streaming tools and multimodal input remain
fail-closed.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from google import genai
from google.genai.types import HttpOptions
from google.oauth2 import service_account

from litestar_gateway.domain.chat_tool_policy import (
    MAX_VERTEX_THOUGHT_SIGNATURE_BYTES,
    VERTEX_GATEWAY_CALL_ID_PREFIX,
    VERTEX_THOUGHT_SIGNATURE_BYPASS,
    decode_vertex_thought_signature,
    validate_chat_request,
)
from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import CredentialMisconfigured, UpstreamResponseInvalid
from litestar_gateway.infrastructure.llm.feature_support import ensure_translatable_chat_request
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.llm.structured_output import parse_response_format

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_FINISH_REASON = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
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
    value = json.loads(arguments)
    assert isinstance(value, dict)
    return value


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _tool_result(content: str) -> dict[str, Any]:
    try:
        decoded = json.loads(content, parse_constant=_reject_non_finite_json)
    except TypeError, ValueError:
        return {"output": content}
    return decoded if isinstance(decoded, dict) else {"output": content}


def _provider_call_id(call_id: str, extra_content: Any) -> str | None:
    gateway = extra_content.get("litestar_gateway") if isinstance(extra_content, dict) else None
    synthetic = (
        isinstance(gateway, dict)
        and set(gateway) == {"synthetic_call_id"}
        and gateway.get("synthetic_call_id") is True
    )
    return None if synthetic else call_id


def _gemini_contents(messages: list[Any]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    provider_call_ids: dict[str, str | None] = {}
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
            parts: list[dict[str, Any]] = []
            current = message
            while True:
                call_id = current["tool_call_id"]
                function_response: dict[str, Any] = {
                    "name": call_names[call_id],
                    "response": _tool_result(current["content"]),
                }
                if provider_call_id := provider_call_ids[call_id]:
                    function_response["id"] = provider_call_id
                parts.append({"function_response": function_response})
                if index >= len(messages):
                    break
                candidate = messages[index]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                current = candidate
                index += 1
            translated.append({"role": "user", "parts": parts})
            continue
        if role not in {"user", "assistant"}:
            continue
        text = _text(message.get("content"))
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            parts = []
            if text:
                parts.append({"text": text})
            for call in tool_calls:
                call_id = call["id"]
                name = call["function"]["name"]
                call_names[call_id] = name
                extra_content = call.get("extra_content")
                provider_call_ids[call_id] = _provider_call_id(call_id, extra_content)
                function_call: dict[str, Any] = {
                    "name": name,
                    "args": _tool_input(call["function"]["arguments"]),
                }
                if provider_call_id := provider_call_ids[call_id]:
                    function_call["id"] = provider_call_id
                part: dict[str, Any] = {"function_call": function_call}
                if isinstance(extra_content, dict):
                    google = extra_content.get("google")
                    if isinstance(google, dict):
                        part["thought_signature"] = decode_vertex_thought_signature(
                            google["thought_signature"],
                            field="tool_calls.extra_content.google.thought_signature",
                        )
                parts.append(part)
            translated.append({"role": "model", "parts": parts})
        else:
            translated.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            )
    return translated


def _gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        function = tool["function"]
        declaration: dict[str, Any] = {"name": function["name"]}
        if "description" in function:
            declaration["description"] = function["description"]
        declaration["parameters_json_schema"] = function.get("parameters", {"type": "object"})
        declarations.append(declaration)
    return [{"function_declarations": declarations}]


def _gemini_tool_config(effective: dict[str, Any]) -> dict[str, Any]:
    choice = effective.get("tool_choice")
    function_calling: dict[str, Any]
    if choice == "none":
        function_calling = {"mode": "NONE"}
    elif choice == "required":
        function_calling = {"mode": "ANY"}
    elif isinstance(choice, dict):
        function_calling = {
            "mode": "ANY",
            "allowed_function_names": [choice["function"]["name"]],
        }
    else:
        function_calling = {"mode": "AUTO"}
    return {"function_calling_config": function_calling}


def to_gemini_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    validate_chat_request(model, request)
    ensure_translatable_chat_request(effective, model.provider.value, allow_tools=True)

    system_parts: list[str] = []
    for message in effective.get("messages", []):
        if isinstance(message, dict) and message.get("role") == "system":
            system_parts.append(_text(message.get("content")))
    contents = _gemini_contents(effective.get("messages") or [])

    config: dict[str, Any] = {}
    if system_parts:
        config["system_instruction"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p"):
        if key in effective:
            config[key] = effective[key]
    if max_tokens := (effective.get("max_tokens") or effective.get("max_completion_tokens")):
        config["max_output_tokens"] = max_tokens
    if (stop := effective.get("stop")) is not None:
        config["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    # Structured output: Gemini emits JSON when response_mime_type is set, and
    # constrains it to a schema via response_schema (a JSON-Schema subset). The
    # JSON comes back as ordinary text in the candidate, so no response reshaping
    # is needed — from_gemini_response already surfaces it as message.content.
    structured = parse_response_format(effective)
    if structured is not None:
        config["response_mime_type"] = "application/json"
        if structured.schema is not None:
            config["response_schema"] = structured.schema
    if isinstance(effective.get("tools"), list):
        config["tools"] = _gemini_tools(effective["tools"])
        config["tool_config"] = _gemini_tool_config(effective)

    return {"model": model.provider_model_id, "contents": contents, "config": config}


def _billable_response(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage_metadata")
    if not isinstance(usage, dict):
        return {"usage": {}}
    prompt = usage.get("prompt_token_count")
    completion = usage.get("candidates_token_count")
    total = usage.get("total_token_count")
    if (
        not isinstance(prompt, int)
        or isinstance(prompt, bool)
        or prompt < 0
        or not isinstance(completion, int)
        or isinstance(completion, bool)
        or completion < 0
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
    ):
        return {"usage": {}}
    return {
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    }


def _invalid_response(response: dict[str, Any], detail: str) -> UpstreamResponseInvalid:
    return UpstreamResponseInvalid(detail, _billable_response(response))


def _gemini_tool_calls(
    response: dict[str, Any],
    parts: list[Any],
    expected_tool_names: set[str] | None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for part in parts:
        if not isinstance(part, dict):
            raise _invalid_response(response, "Vertex returned malformed content")
        function_call = part.get("function_call")
        signature = part.get("thought_signature")
        if function_call is None:
            if signature is not None and (
                not isinstance(part.get("text"), str)
                or not isinstance(signature, bytes)
                or not signature
                or len(signature) > MAX_VERTEX_THOUGHT_SIGNATURE_BYTES
                or signature == VERTEX_THOUGHT_SIGNATURE_BYPASS
            ):
                raise _invalid_response(
                    response,
                    "Vertex returned a malformed non-function thought signature",
                )
            continue
        if not isinstance(function_call, dict):
            raise _invalid_response(response, "Vertex returned a malformed function call")
        name = function_call.get("name")
        arguments = function_call.get("args")
        call_id = function_call.get("id")
        synthetic_call_id = call_id is None
        if call_id is None:
            call_id = f"{VERTEX_GATEWAY_CALL_ID_PREFIX}{uuid4().hex}"
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in seen_ids
            or not isinstance(name, str)
            or not name
            or (expected_tool_names is not None and name not in expected_tool_names)
            or not isinstance(arguments, dict)
        ):
            raise _invalid_response(response, "Vertex returned a malformed function call")
        if signature is not None and (
            not isinstance(signature, bytes)
            or not signature
            or len(signature) > MAX_VERTEX_THOUGHT_SIGNATURE_BYTES
            or signature == VERTEX_THOUGHT_SIGNATURE_BYPASS
            or bool(calls)
        ):
            raise _invalid_response(
                response,
                "Vertex returned a malformed function-call thought signature",
            )
        try:
            serialized_arguments = json.dumps(
                arguments,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise _invalid_response(response, "Vertex returned non-JSON tool arguments") from exc
        seen_ids.add(call_id)
        call: dict[str, Any] = {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": serialized_arguments},
        }
        extra_content: dict[str, Any] = {}
        if signature is not None:
            extra_content["google"] = {
                "thought_signature": base64.b64encode(signature).decode("ascii")
            }
        if synthetic_call_id:
            extra_content["litestar_gateway"] = {"synthetic_call_id": True}
        if extra_content:
            call["extra_content"] = extra_content
        calls.append(call)
    return calls


def from_gemini_response(
    response: dict[str, Any],
    *,
    expected_tool_names: set[str] | None = None,
    require_tool_call: bool = False,
    forbid_tool_call: bool = False,
    require_thought_signature: bool = False,
) -> dict[str, Any]:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise _invalid_response(response, "Vertex returned malformed candidates")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise _invalid_response(response, "Vertex returned a malformed candidate")
    content = candidate.get("content")
    if not isinstance(content, dict) or content.get("role") not in (None, "model"):
        raise _invalid_response(response, "Vertex returned malformed content")
    finish_raw = candidate.get("finish_reason")
    if not isinstance(finish_raw, str) or finish_raw not in _FINISH_REASON:
        raise _invalid_response(response, "Vertex returned an invalid finish reason")
    finish_reason = _FINISH_REASON[finish_raw]
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise _invalid_response(response, "Vertex returned malformed content")
    usage = _billable_response(response)["usage"]
    if not usage:
        raise _invalid_response(response, "Vertex returned malformed usage")
    tool_calls = _gemini_tool_calls(response, parts, expected_tool_names)
    if require_tool_call and not tool_calls:
        raise _invalid_response(response, "Vertex omitted a required tool call")
    if forbid_tool_call and tool_calls:
        raise _invalid_response(response, "Vertex returned a tool call for tool_choice='none'")
    if require_thought_signature and tool_calls:
        first_extra = tool_calls[0].get("extra_content")
        if not isinstance(first_extra, dict) or "google" not in first_extra:
            raise _invalid_response(
                response,
                "Vertex omitted the required Gemini 3 function-call thought signature",
            )
    text = "".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": response.get("response_id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.get("model_version"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": ("tool_calls" if tool_calls else finish_reason),
            }
        ],
        "usage": usage,
    }


def _tool_response_contract(
    request: dict[str, Any], model: Model
) -> tuple[set[str], bool, bool, bool]:
    effective = model.merge_params(request)
    tools = effective.get("tools")
    names = (
        {
            tool["function"]["name"]
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
        if isinstance(tools, list)
        else set()
    )
    choice = effective.get("tool_choice")
    if isinstance(choice, dict):
        names = {choice["function"]["name"]}
    require_tool_call = choice == "required" or isinstance(choice, dict)
    forbid_tool_call = choice == "none"
    require_signature = bool(names) and model.provider_model_id.lower().startswith("gemini-3")
    return names, require_tool_call, forbid_tool_call, require_signature


def gemini_chunk_to_delta(chunk: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Map one Gemini stream chunk to an OpenAI chunk (delta, finish_reason)."""
    candidate = (chunk.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
    finish_raw = candidate.get("finish_reason")
    finish = _FINISH_REASON.get(finish_raw, "stop") if finish_raw else None
    delta = {"content": text} if text else None
    return delta, finish


def to_gemini_embed_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    return {"model": model.provider_model_id, "contents": effective.get("input")}


def from_gemini_embeddings(response: dict[str, Any], model_id: str) -> dict[str, Any]:
    embeddings = response.get("embeddings") or []
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": e.get("values") if isinstance(e, dict) else [],
        }
        for i, e in enumerate(embeddings)
    ]
    # Bill from the provider's reported token count where available (H14). When
    # google-genai omits usage_metadata, leave the fields None so the meter's
    # non-streaming estimation fallback kicks in rather than billing zero.
    usage = response.get("usage_metadata") or {}
    prompt = usage.get("prompt_token_count") or usage.get("total_token_count")
    total = usage.get("total_token_count") or prompt
    return {
        "object": "list",
        "data": data,
        "model": model_id,
        "usage": {"prompt_tokens": prompt, "total_tokens": total},
    }


def to_imagen_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    return {"model": model.provider_model_id, "prompt": effective.get("prompt")}


def from_imagen_response(response: dict[str, Any]) -> dict[str, Any]:
    data = []
    for generated in response.get("generated_images") or []:
        image = (generated.get("image") or {}) if isinstance(generated, dict) else {}
        raw = image.get("image_bytes")
        if raw is not None:
            b64 = raw if isinstance(raw, str) else base64.b64encode(raw).decode("ascii")
            data.append({"b64_json": b64})
        elif image.get("gcs_uri"):
            data.append({"url": image["gcs_uri"]})
    return {"created": int(time.time()), "data": data}


async def _raw_request(
    client: genai.Client, http_method: str, path: str, body: dict[str, Any]
) -> Any:
    """Single call-site for the private `google-genai` raw-request surface.

    The native passthrough (`agenerate_content`) needs to send a
    `GenerateContentRequest` body upstream verbatim, but `google-genai` has no
    public raw-request API — only the private `client.aio._api_client.async_request`
    (see ISSUE-005). Routing both native passthrough call sites through this one
    helper keeps the private-API surface to a single, documented spot, and
    `tests/native/test_genai_private_api_contract.py` pins the assumed shape
    against the *installed* SDK so a `google-genai` upgrade that renames/re-signs
    this method fails in CI instead of 500ing in production.
    """
    return await client.aio._api_client.async_request(http_method, path, body)


async def _raw_request_streamed(
    client: genai.Client, http_method: str, path: str, body: dict[str, Any]
) -> Any:
    """Streaming counterpart of `_raw_request` — see its docstring for why this
    wraps a private `google-genai` method behind a single, tested call-site."""
    return await client.aio._api_client.async_request_streamed(http_method, path, body)


def _build_client(credentials: dict[str, str], timeout_ms: int) -> genai.Client:
    creds = None
    if raw := credentials.get("vertex_credentials"):
        # Never let a malformed service-account JSON surface as a 500 whose message
        # could echo the credential's private-key material.
        try:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(raw), scopes=[_SCOPE]
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise CredentialMisconfigured(
                "vertex_credentials is not a valid service-account JSON"
            ) from exc
    return genai.Client(
        vertexai=True,
        project=credentials.get("vertex_project"),
        location=credentials.get("vertex_location"),
        credentials=creds,
        http_options=HttpOptions(timeout=timeout_ms),
    )


class VertexAdapter:
    def __init__(self, resilience: ResilienceConfig | None = None) -> None:
        self._resilience = resilience or ResilienceConfig()

    def _client(self, credentials: dict[str, str]) -> genai.Client:
        return _build_client(credentials, self._resilience.timeout_ms)

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Each genai.Client owns an httpx connection pool; close it after the call
        # so per-request clients don't leak sockets/file descriptors (same pattern
        # as the OpenAI/Anthropic adapters).
        client = self._client(credentials)
        try:
            response = client.models.generate_content(**to_gemini_request(request, model))
            names, require_call, forbid_call, require_signature = _tool_response_contract(
                request, model
            )
            return from_gemini_response(
                response.model_dump(),
                expected_tool_names=names,
                require_tool_call=require_call,
                forbid_tool_call=forbid_call,
                require_thought_signature=require_signature,
            )
        finally:
            client.close()

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client(credentials)
        try:
            response = await client.aio.models.generate_content(**to_gemini_request(request, model))
            names, require_call, forbid_call, require_signature = _tool_response_contract(
                request, model
            )
            return from_gemini_response(
                response.model_dump(),
                expected_tool_names=names,
                require_tool_call=require_call,
                forbid_tool_call=forbid_call,
                require_thought_signature=require_signature,
            )
        finally:
            await client.aio.aclose()

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        client = self._client(credentials)
        base = {
            "id": "chatcmpl-gemini",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model.provider_model_id,
        }
        # Keep the client open for the whole stream; close on completion/disconnect
        # (finally runs on generator close).
        try:
            # Open the provider stream first, so a start-of-stream failure
            # surfaces as an HTTP status (via priming) instead of after a
            # fabricated "started" chunk (R7-H24). Only then synthesize the role
            # delta Gemini has no separate "start" event for.
            stream: Any = await client.aio.models.generate_content_stream(
                **to_gemini_request(request, model)
            )
            yield {
                **base,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            # Gemini reports (cumulative) usage_metadata on stream chunks; keep
            # the latest and emit a trailing OpenAI-style usage chunk so
            # streamed calls can be metered.
            usage_metadata: dict[str, Any] = {}
            async for chunk in stream:
                raw = chunk.model_dump()
                if raw.get("usage_metadata"):
                    usage_metadata = raw["usage_metadata"]
                delta, finish = gemini_chunk_to_delta(raw)
                if delta is None and finish is None:
                    continue
                yield {
                    **base,
                    "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                }
            prompt_tokens = usage_metadata.get("prompt_token_count") or 0
            completion_tokens = usage_metadata.get("candidates_token_count") or 0
            yield {
                **base,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": usage_metadata.get("total_token_count")
                    or (prompt_tokens + completion_tokens),
                },
            }
        finally:
            await client.aio.aclose()

    async def agenerate_content(
        self, native_body: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Native passthrough: POST the client's Gemini `GenerateContentRequest` body
        # upstream verbatim and return the raw REST response as-is — NO
        # to_gemini_request / from_gemini_response translation. The model alias is
        # resolved to the provider id in the URL PATH (Gemini carries the model in
        # the path, not the body), which is not translation. The credential-side
        # config (project/location/base_url) stays server-side in the client.
        client = self._client(credentials)
        path = f"{model.provider_model_id}:generateContent"
        try:
            response = await _raw_request(client, "post", path, dict(native_body))
            return json.loads(response.body) if response.body else {}
        finally:
            await client.aio.aclose()

    async def astream_generate_content(
        self, native_body: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        # Native passthrough streaming: relay the upstream Gemini
        # `GenerateContentResponse` chunks verbatim (parsed from the raw REST SSE)
        # — NO gemini_chunk_to_delta, NO OpenAI chunk shape. Only the model alias is
        # resolved to the provider id in the URL PATH (not translation). Client
        # lifecycle mirrors astream_chat_completion minus the translation.
        client = self._client(credentials)
        path = f"{model.provider_model_id}:streamGenerateContent"
        try:
            stream = await _raw_request_streamed(client, "post", path, dict(native_body))
            async for chunk in stream:
                if chunk.body:
                    yield json.loads(chunk.body)
        finally:
            await client.aio.aclose()

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client(credentials)
        try:
            response = client.models.embed_content(**to_gemini_embed_request(request, model))
            return from_gemini_embeddings(response.model_dump(), model.provider_model_id)
        finally:
            client.close()

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client(credentials)
        try:
            response = await client.aio.models.embed_content(
                **to_gemini_embed_request(request, model)
            )
            return from_gemini_embeddings(response.model_dump(), model.provider_model_id)
        finally:
            await client.aio.aclose()

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client(credentials)
        try:
            response = client.models.generate_images(**to_imagen_request(request, model))
            return from_imagen_response(response.model_dump())
        finally:
            client.close()

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client(credentials)
        try:
            response = await client.aio.models.generate_images(**to_imagen_request(request, model))
            return from_imagen_response(response.model_dump())
        finally:
            await client.aio.aclose()
