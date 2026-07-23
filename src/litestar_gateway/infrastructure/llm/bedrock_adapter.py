"""AWS Bedrock adapter: OpenAI chat.completions ↔ the Converse API, plus
`invoke_model` embeddings (Titan, Cohere) and images (Titan Image Generator).

Pure translators (`to_converse_request` / `from_converse_response`) do the
schema work; the adapter is a thin boto3 wrapper. Responses are provided by
wrapping this adapter in `ChatToResponsesAdapter`.

boto3 is synchronous: the async surface delegates to a worker thread on a
dedicated bounded executor, including the streaming EventStream (iterated one
event per thread hop so the event loop is never blocked).

Scope: text-in/text-out, best-effort JSON-object prompting, and faithful
non-streaming client tool calls. JSON-schema output, streaming client tools,
and multimodal content remain fail-closed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from litestar_gateway.domain.chat_tool_policy import validate_chat_request
from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import (
    CredentialMisconfigured,
    UnsupportedOperation,
    UpstreamResponseInvalid,
)
from litestar_gateway.infrastructure.llm.feature_support import ensure_translatable_chat_request
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.llm.structured_output import parse_response_format

# Titan has no batch embeddings endpoint (one invoke_model call per text), so
# multi-input requests fan out concurrently; bound the parallelism so a large
# batch cannot monopolize the worker-thread pool or upstream connections.
_TITAN_EMBED_FANOUT = 8

_FINISH_REASON = {
    "end_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "stop_sequence": "stop",
    # Forced structured-output tool only (same rationale as the Anthropic
    # adapter): the JSON is surfaced as message.content, so the client sees a
    # normal completion — report "stop", not "tool_calls".
    "tool_use": "stop",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}
_STREAM_FINISH_REASON = {
    reason: finish for reason, finish in _FINISH_REASON.items() if reason != "tool_use"
}
_STREAM_EVENT_KINDS = frozenset(
    {
        "messageStart",
        "contentBlockStart",
        "contentBlockDelta",
        "contentBlockStop",
        "messageStop",
        "metadata",
    }
)
_BEDROCK_TOOL_USE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,64}$")


# Streaming does one thread hop per EventStream event, so a few concurrent
# Bedrock streams issue many tiny hops; a dedicated bounded pool keeps them
# from saturating the loop's process-wide default executor and slowing
# unrelated requests (R6-M45). Module-level because the adapter is built once
# per LLMGatewayImpl registry: one shared pool covers every instance.
_BEDROCK_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bedrock")

# The Titan embed fan-out (`_titan_aembeddings`) can submit up to
# `_TITAN_EMBED_FANOUT` concurrent invoke_model calls for a single request. If
# those shared `_BEDROCK_EXECUTOR`, one large embeddings request could occupy
# every worker for the duration of its network round trips, leaving no worker
# free for other teams' concurrent chat streams' `_next_event` pulls (R7-M58).
# A small dedicated pool for the embed fan-out keeps the event-pull pool
# uncontended; sized to the fan-out bound since the semaphore never lets more
# than that many embed calls run at once anyway.
_BEDROCK_EMBED_EXECUTOR = ThreadPoolExecutor(
    max_workers=_TITAN_EMBED_FANOUT, thread_name_prefix="bedrock-embed"
)


async def _run[T](func: Callable[..., T], /, *args: Any) -> T:
    """Run a blocking boto3 call on the dedicated Bedrock executor."""
    return await asyncio.get_running_loop().run_in_executor(_BEDROCK_EXECUTOR, func, *args)


async def _run_embed[T](func: Callable[..., T], /, *args: Any) -> T:
    """Run a blocking boto3 call on the dedicated Titan embed-fanout executor
    (see `_BEDROCK_EMBED_EXECUTOR` above for why it's kept separate)."""
    return await asyncio.get_running_loop().run_in_executor(_BEDROCK_EMBED_EXECUTOR, func, *args)


def _next_event(events: Iterator[dict[str, Any]]) -> dict[str, Any] | None:
    """One EventStream pull, run in a worker thread (boto3 streams are sync).
    Events are always dicts, so None is a safe end-of-stream marker."""
    return next(events, None)


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


def _bedrock_messages(messages: list[Any]) -> list[dict[str, Any]]:
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
                        "toolResult": {
                            "toolUseId": current["tool_call_id"],
                            "content": [{"text": current["content"]}],
                        }
                    }
                )
                if index >= len(messages):
                    break
                following = messages[index]
                if not isinstance(following, dict) or following.get("role") != "tool":
                    break
                current = following
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
                content.append({"text": text})
            content.extend(
                {
                    "toolUse": {
                        "toolUseId": call["id"],
                        "name": call["function"]["name"],
                        "input": _tool_input(call["function"]["arguments"]),
                    }
                }
                for call in tool_calls
            )
            translated.append({"role": "assistant", "content": content})
        else:
            translated.append({"role": role, "content": [{"text": text}]})
    return translated


def _bedrock_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    for tool in tools:
        function = tool["function"]
        parameters = function.get("parameters")
        spec: dict[str, Any] = {
            "name": function["name"],
            "inputSchema": {"json": parameters if parameters is not None else {"type": "object"}},
        }
        if "description" in function:
            spec["description"] = function["description"]
        translated.append({"toolSpec": spec})
    return translated


def _bedrock_tool_choice(effective: dict[str, Any]) -> dict[str, Any] | None:
    choice = effective.get("tool_choice")
    if choice == "auto":
        return {"auto": {}}
    if choice == "required":
        return {"any": {}}
    if isinstance(choice, dict):
        return {"tool": {"name": choice["function"]["name"]}}
    return None


def _client_tool_names(effective: dict[str, Any]) -> set[str] | None:
    tools = effective.get("tools")
    if not isinstance(tools, list):
        return None
    return {tool["function"]["name"] for tool in tools}


def to_converse_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    validate_chat_request(model, request)
    ensure_translatable_chat_request(effective, model.provider.value, allow_tools=True)
    structured = parse_response_format(effective)

    system_parts: list[str] = []
    for message in effective.get("messages", []):
        role = message.get("role")
        if role == "system":
            system_parts.append(_text(message.get("content")))
    messages = _bedrock_messages(effective.get("messages") or [])

    # json_object (no schema): Converse has no JSON mode, so nudge via system.
    if structured is not None and structured.schema is None:
        system_parts.append("Respond with a single valid JSON object and nothing else.")

    kwargs: dict[str, Any] = {"modelId": model.provider_model_id, "messages": messages}
    if system_parts:
        kwargs["system"] = [{"text": part} for part in system_parts]

    inference: dict[str, Any] = {}
    max_tokens = effective.get("max_tokens") or effective.get("max_completion_tokens")
    if max_tokens is not None:
        inference["maxTokens"] = max_tokens
    if "temperature" in effective:
        inference["temperature"] = effective["temperature"]
    if "top_p" in effective:
        inference["topP"] = effective["top_p"]
    if (stop := effective.get("stop")) is not None:
        inference["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)
    if inference:
        kwargs["inferenceConfig"] = inference

    if isinstance(effective.get("tools"), list):
        tool_config: dict[str, Any] = {"tools": _bedrock_tools(effective["tools"])}
        if choice := _bedrock_tool_choice(effective):
            tool_config["toolChoice"] = choice
        kwargs["toolConfig"] = tool_config
    return kwargs


def _billable_response(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {"usage": {}}
    prompt = usage.get("inputTokens")
    completion = usage.get("outputTokens")
    if (
        not isinstance(prompt, int)
        or isinstance(prompt, bool)
        or prompt < 0
        or not isinstance(completion, int)
        or isinstance(completion, bool)
        or completion < 0
    ):
        return {"usage": {}}
    return {
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    }


def _invalid_response(response: dict[str, Any], detail: str) -> UpstreamResponseInvalid:
    return UpstreamResponseInvalid(detail, _billable_response(response))


def _tool_calls(
    response: dict[str, Any],
    blocks: list[Any],
    expected_names: set[str],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for block in blocks:
        tool_use = block.get("toolUse") if isinstance(block, dict) else None
        if tool_use is None:
            continue
        call_id = tool_use.get("toolUseId") if isinstance(tool_use, dict) else None
        name = tool_use.get("name") if isinstance(tool_use, dict) else None
        tool_input = tool_use.get("input") if isinstance(tool_use, dict) else None
        if (
            not isinstance(call_id, str)
            or _BEDROCK_TOOL_USE_ID.fullmatch(call_id) is None
            or call_id in seen_ids
            or set(tool_use) != {"toolUseId", "name", "input"}
            or not isinstance(name, str)
            or name not in expected_names
            or not isinstance(tool_input, dict)
        ):
            raise _invalid_response(response, "Bedrock returned a malformed tool call")
        try:
            arguments = json.dumps(
                tool_input,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise _invalid_response(response, "Bedrock returned non-JSON tool arguments") from exc
        seen_ids.add(call_id)
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return calls


def from_converse_response(
    response: dict[str, Any],
    model_id: str,
    *,
    client_tool_names: set[str] | None = None,
    require_tool_call: bool = False,
    required_tool_name: str | None = None,
) -> dict[str, Any]:
    output = response.get("output")
    message = output.get("message") if isinstance(output, dict) else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _invalid_response(response, "Bedrock returned malformed output")
    blocks = message.get("content")
    if not isinstance(blocks, list):
        raise _invalid_response(response, "Bedrock returned malformed content")
    stop_reason = response.get("stopReason")
    if not isinstance(stop_reason, str) or stop_reason not in _FINISH_REASON:
        raise _invalid_response(response, "Bedrock returned an invalid stop reason")
    billable = _billable_response(response)["usage"]
    if not billable:
        raise _invalid_response(response, "Bedrock returned malformed usage")
    text_parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or set(block) not in ({"text"}, {"toolUse"}):
            raise _invalid_response(response, "Bedrock returned malformed content")
        if "text" in block:
            if not isinstance(block["text"], str):
                raise _invalid_response(response, "Bedrock returned malformed text content")
            text_parts.append(block["text"])
    text = "".join(text_parts)
    calls = (
        _tool_calls(response, blocks, client_tool_names) if client_tool_names is not None else []
    )
    if required_tool_name is not None and (
        not calls or any(call["function"]["name"] != required_tool_name for call in calls)
    ):
        raise _invalid_response(response, "Bedrock violated the named tool choice")
    if require_tool_call and not calls:
        raise _invalid_response(response, "Bedrock omitted a required tool call")
    if client_tool_names is not None and bool(calls) != (stop_reason == "tool_use"):
        raise _invalid_response(response, "Bedrock returned an inconsistent tool response")
    if client_tool_names is None and stop_reason == "tool_use":
        raise _invalid_response(response, "Bedrock returned an unexpected tool stop")
    if client_tool_names is None and any(
        isinstance(block, dict) and "toolUse" in block for block in blocks
    ):
        raise _invalid_response(response, "Bedrock returned an unexpected tool call")
    chat_message: dict[str, Any] = {
        "role": "assistant",
        "content": text if text or not calls else None,
    }
    if calls:
        chat_message["tool_calls"] = calls
    return {
        "id": "chatcmpl-bedrock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": chat_message,
                "finish_reason": "tool_calls" if calls else _FINISH_REASON[stop_reason],
            }
        ],
        "usage": billable,
    }


def _invalid_stream(detail: str) -> UpstreamResponseInvalid:
    return UpstreamResponseInvalid(detail, {"usage": {}})


def _stream_event_payload(event: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(event, dict) or len(event) != 1:
        raise _invalid_stream("Bedrock returned a malformed stream event union")
    kind = next(iter(event))
    payload = event[kind]
    if kind not in _STREAM_EVENT_KINDS:
        raise _invalid_stream("Bedrock returned an unknown stream event")
    if not isinstance(payload, dict):
        raise _invalid_stream(f"Bedrock returned malformed {kind}")
    return kind, payload


def _stream_index(payload: dict[str, Any], *, fields: set[str], kind: str) -> None:
    index = payload.get("contentBlockIndex")
    if set(payload) != fields or not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise _invalid_stream(f"Bedrock returned malformed {kind}")


def _validate_stream_metadata(payload: dict[str, Any]) -> None:
    allowed = {"usage", "metrics", "trace", "performanceConfig", "serviceTier"}
    usage_allowed = {
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "cacheDetails",
    }
    usage = payload.get("usage")
    metrics = payload.get("metrics")
    if (
        not {"usage", "metrics"} <= set(payload) <= allowed
        or not isinstance(usage, dict)
        or not {"inputTokens", "outputTokens", "totalTokens"} <= set(usage) <= usage_allowed
        or not isinstance(metrics, dict)
        or set(metrics) != {"latencyMs"}
        or not isinstance(metrics.get("latencyMs"), int)
        or isinstance(metrics.get("latencyMs"), bool)
        or metrics["latencyMs"] < 0
    ):
        raise _invalid_stream("Bedrock returned malformed stream metadata")
    for field in ("inputTokens", "outputTokens", "totalTokens"):
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _invalid_stream("Bedrock returned malformed stream usage")
    for field in ("cacheReadInputTokens", "cacheWriteInputTokens"):
        value = usage.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise _invalid_stream("Bedrock returned malformed stream usage")
    if "cacheDetails" in usage and not isinstance(usage["cacheDetails"], list):
        raise _invalid_stream("Bedrock returned malformed stream usage")
    for field in ("trace", "performanceConfig", "serviceTier"):
        if field in payload and not isinstance(payload[field], dict):
            raise _invalid_stream("Bedrock returned malformed stream metadata")


def converse_event_to_delta(event: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Map one Converse stream event to an OpenAI chunk (delta, finish_reason).

    Returns (None, None) for events that produce no chunk (block start/stop,
    metadata — usage is read by the caller).
    """
    kind, payload = _stream_event_payload(event)
    if kind == "messageStart":
        if set(payload) != {"role"} or payload.get("role") != "assistant":
            raise _invalid_stream("Bedrock returned an invalid stream message role")
        return {"role": "assistant"}, None
    if kind == "contentBlockStart":
        _stream_index(
            payload,
            fields={"start", "contentBlockIndex"},
            kind=kind,
        )
        raise _invalid_stream("Bedrock returned an unsupported streaming content block")
    if kind == "contentBlockDelta":
        _stream_index(
            payload,
            fields={"delta", "contentBlockIndex"},
            kind=kind,
        )
        delta = payload.get("delta")
        if not isinstance(delta, dict) or len(delta) != 1:
            raise _invalid_stream("Bedrock returned malformed contentBlockDelta")
        if set(delta) == {"text"} and isinstance(delta["text"], str):
            return {"content": delta["text"]}, None
        raise _invalid_stream("Bedrock returned an unsupported streaming content delta")
    if kind == "contentBlockStop":
        _stream_index(payload, fields={"contentBlockIndex"}, kind=kind)
        return None, None
    if kind == "messageStop":
        if (
            not {"stopReason"}
            <= set(payload)
            <= {
                "stopReason",
                "additionalModelResponseFields",
            }
        ):
            raise _invalid_stream("Bedrock returned malformed messageStop")
        if "additionalModelResponseFields" in payload and not isinstance(
            payload["additionalModelResponseFields"], dict
        ):
            raise _invalid_stream("Bedrock returned malformed messageStop")
        reason = payload.get("stopReason")
        if not isinstance(reason, str) or reason not in _STREAM_FINISH_REASON:
            raise _invalid_stream("Bedrock returned an invalid stream stop reason")
        return {}, _STREAM_FINISH_REASON[reason]
    _validate_stream_metadata(payload)
    return None, None


def to_titan_image_body(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    config: dict[str, Any] = {"numberOfImages": effective.get("n") or 1}
    size = effective.get("size")
    if isinstance(size, str) and size.count("x") == 1:
        width, height = size.split("x")
        if width.isdigit() and height.isdigit():
            config["width"] = int(width)
            config["height"] = int(height)
    return {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": effective.get("prompt")},
        "imageGenerationConfig": config,
    }


def from_titan_image_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "created": int(time.time()),
        "data": [{"b64_json": image} for image in payload.get("images") or []],
    }


def _require_aws(credentials: dict[str, str]) -> tuple[str, str, str, str | None]:
    missing = [
        key
        for key in ("region", "aws_access_key_id", "aws_secret_access_key")
        if not credentials.get(key)
    ]
    if missing:
        raise CredentialMisconfigured(f"bedrock credential is missing: {', '.join(missing)}")
    return (
        credentials["region"],
        credentials["aws_access_key_id"],
        credentials["aws_secret_access_key"],
        credentials.get("aws_session_token") or None,
    )


def _titan_embeddings_response(payloads: list[dict[str, Any]], model_id: str) -> dict[str, Any]:
    data = []
    prompt_tokens = 0
    counted = False
    for index, payload in enumerate(payloads):
        data.append(
            {
                "object": "embedding",
                "index": index,
                "embedding": payload.get("embedding") or [],
            }
        )
        count = payload.get("inputTextTokenCount")
        if isinstance(count, int):
            prompt_tokens += count
            counted = True
    return {
        "object": "list",
        "data": data,
        "model": model_id,
        "usage": {
            "prompt_tokens": prompt_tokens if counted else None,
            "total_tokens": prompt_tokens if counted else None,
        },
    }


def _embedding_inputs(effective: dict[str, Any]) -> list[str]:
    raw = effective.get("input")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [text for text in raw if isinstance(text, str)]
    return []


class BedrockAdapter:
    def __init__(self, resilience: ResilienceConfig | None = None) -> None:
        self._resilience = resilience or ResilienceConfig()

    def _client(self, credentials: dict[str, str]) -> Any:
        # Region and keys come from the (admin-managed) credential only, never
        # from the model — same endpoint-provenance rule as the other adapters.
        region, key_id, secret, session_token = _require_aws(credentials)
        config = BotoConfig(
            read_timeout=self._resilience.timeout,
            connect_timeout=self._resilience.timeout,
            retries={"max_attempts": self._resilience.max_retries, "mode": "standard"},
        )
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            aws_session_token=session_token,
            config=config,
        )

    # ── chat ────────────────────────────────────────────────────────────────

    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        # Close the client after the call so its connection pool isn't leaked.
        client = self._client(credentials)
        try:
            response = client.converse(**to_converse_request(request, model))
            effective = model.merge_params(request)
            client_tool_names = _client_tool_names(effective)
            choice = effective.get("tool_choice")
            required_tool_name = choice["function"]["name"] if isinstance(choice, dict) else None
            return from_converse_response(
                response,
                model.provider_model_id,
                client_tool_names=client_tool_names,
                require_tool_call=choice == "required" or required_tool_name is not None,
                required_tool_name=required_tool_name,
            )
        finally:
            client.close()

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await _run(self.chat_completion, request, model, credentials)

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        client = await _run(self._client, credentials)
        base = {
            "id": "chatcmpl-bedrock",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model.provider_model_id,
        }
        # Converse reports usage on the trailing metadata event; accumulate it
        # and emit an OpenAI-style usage chunk so streamed calls can be metered.
        input_tokens = 0
        output_tokens = 0
        # Keep the client open for the whole stream; close on completion/disconnect.
        try:
            response = await _run(
                lambda: client.converse_stream(**to_converse_request(request, model))
            )
            events: Iterator[dict[str, Any]] = iter(response["stream"])
            while True:
                event = await _run(_next_event, events)
                if event is None:
                    break
                delta, finish = converse_event_to_delta(event)
                if "metadata" in event:
                    usage = event["metadata"]["usage"]
                    input_tokens = usage["inputTokens"]
                    output_tokens = usage["outputTokens"]
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
            await _run(client.close)

    # ── embeddings (invoke_model) ───────────────────────────────────────────

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        effective = model.merge_params(request)
        texts = _embedding_inputs(effective)
        model_id = model.provider_model_id
        client = self._client(credentials)
        try:
            if model_id.startswith("amazon.titan-embed"):
                # Titan embeds one text per call.
                payloads = [_invoke(client, model_id, {"inputText": text}) for text in texts]
                return _titan_embeddings_response(payloads, model_id)
            if model_id.startswith("cohere.embed"):
                payload = _invoke(
                    client, model_id, {"texts": texts, "input_type": "search_document"}
                )
                data = [
                    {"object": "embedding", "index": index, "embedding": embedding}
                    for index, embedding in enumerate(payload.get("embeddings") or [])
                ]
                return {
                    "object": "list",
                    "data": data,
                    "model": model_id,
                    # Cohere reports no token count: leave usage None so the meter's
                    # estimation fallback kicks in rather than billing zero (H14).
                    "usage": {"prompt_tokens": None, "total_tokens": None},
                }
            raise UnsupportedOperation(
                f"Bedrock embeddings for '{model_id}' are not supported"
                " (amazon.titan-embed-* and cohere.embed-* only)"
            )
        finally:
            client.close()

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        if model.provider_model_id.startswith("amazon.titan-embed"):
            return await self._titan_aembeddings(request, model, credentials)
        return await _run(self.embeddings, request, model, credentials)

    async def _titan_aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        """Titan embeds one text per call: fan the per-text invokes out over
        worker threads instead of paying N sequential round trips."""
        effective = model.merge_params(request)
        texts = _embedding_inputs(effective)
        model_id = model.provider_model_id
        client = await _run(self._client, credentials)
        semaphore = asyncio.Semaphore(_TITAN_EMBED_FANOUT)

        async def embed_one(text: str) -> dict[str, Any]:
            async with semaphore:
                return await _run_embed(_invoke, client, model_id, {"inputText": text})

        try:
            # return_exceptions so every in-flight call finishes before the
            # client is closed; the first failure in input order is re-raised
            # below, matching the sequential path.
            results = await asyncio.gather(
                *(embed_one(text) for text in texts), return_exceptions=True
            )
        finally:
            await _run(client.close)
        payloads: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            payloads.append(result)
        return _titan_embeddings_response(payloads, model_id)

    # ── images (invoke_model, Titan Image Generator) ────────────────────────

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        model_id = model.provider_model_id
        if not model_id.startswith("amazon.titan-image"):
            raise UnsupportedOperation(
                f"Bedrock image generation for '{model_id}' is not supported"
                " (amazon.titan-image-* only)"
            )
        client = self._client(credentials)
        try:
            payload = _invoke(client, model_id, to_titan_image_body(request, model))
        finally:
            client.close()
        return from_titan_image_response(payload)

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        return await _run(self.images, request, model, credentials)


def _invoke(client: Any, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())
