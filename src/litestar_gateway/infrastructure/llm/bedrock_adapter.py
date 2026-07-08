"""AWS Bedrock adapter: OpenAI chat.completions ↔ the Converse API, plus
`invoke_model` embeddings (Titan, Cohere) and images (Titan Image Generator).

Pure translators (`to_converse_request` / `from_converse_response`) do the
schema work; the adapter is a thin boto3 wrapper. Responses are provided by
wrapping this adapter in `ChatToResponsesAdapter`.

boto3 is synchronous: the async surface delegates to a worker thread on a
dedicated bounded executor, including the streaming EventStream (iterated one
event per thread hop so the event loop is never blocked).

Scope: text-in/text-out, plus structured outputs (`response_format`) translated
to a forced tool — streaming included (partial tool-input JSON is relayed as
content). Not yet translated: general tool/function calling and multimodal
content.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import CredentialMisconfigured, UnsupportedOperation
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.llm.structured_output import parse_response_format

# Titan has no batch embeddings endpoint (one invoke_model call per text), so
# multi-input requests fan out concurrently; bound the parallelism so a large
# batch cannot monopolize the worker-thread pool or upstream connections.
_TITAN_EMBED_FANOUT = 8

_FINISH_REASON = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    # Forced structured-output tool only (same rationale as the Anthropic
    # adapter): the JSON is surfaced as message.content, so the client sees a
    # normal completion — report "stop", not "tool_calls".
    "tool_use": "stop",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}


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


def to_converse_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    structured = parse_response_format(effective)

    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in effective.get("messages", []):
        role = message.get("role")
        text = _text(message.get("content"))
        if role == "system":
            system_parts.append(text)
        elif role in ("user", "assistant"):
            messages.append({"role": role, "content": [{"text": text}]})
        # tool/function messages are ignored in this first cut

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

    # json_schema: force a single tool whose input schema is the requested one,
    # so the model must return a matching toolUse block. from_converse_response
    # surfaces that input as the (JSON) message content.
    if structured is not None and structured.schema is not None:
        kwargs["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": structured.name,
                        "description": "Return the result as JSON matching the schema.",
                        "inputSchema": {"json": structured.schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": structured.name}},
        }
    return kwargs


def from_converse_response(response: dict[str, Any], model_id: str) -> dict[str, Any]:
    message = (response.get("output") or {}).get("message") or {}
    blocks = message.get("content") or []
    text = "".join(
        block["text"]
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
    # Forced structured-output tool: the JSON is the toolUse input, not a text
    # block. Serialize it into content so the client sees the same
    # JSON-in-content shape it gets natively from OpenAI's response_format.
    if not text:
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("toolUse"), dict):
                text = json.dumps(block["toolUse"].get("input") or {})
                break
    usage = response.get("usage") or {}
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    return {
        "id": "chatcmpl-bedrock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _FINISH_REASON.get(response.get("stopReason", ""), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": usage.get("totalTokens") or (input_tokens or 0) + (output_tokens or 0),
        },
    }


def converse_event_to_delta(event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Map one Converse stream event to an OpenAI chunk (delta, finish_reason).

    Returns (None, None) for events that produce no chunk (block start/stop,
    metadata — usage is read by the caller).
    """
    if "messageStart" in event:
        return {"role": event["messageStart"].get("role", "assistant")}, None
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta") or {}
        if isinstance(delta.get("text"), str):
            return {"content": delta["text"]}, None
        tool = delta.get("toolUse")
        if isinstance(tool, dict) and isinstance(tool.get("input"), str):
            # Structured output streams as a forced tool: relay its partial JSON
            # as content deltas so the client reconstructs the same JSON it gets
            # non-streamed.
            return {"content": tool["input"]}, None
        return None, None
    if "messageStop" in event:
        return {}, _FINISH_REASON.get(event["messageStop"].get("stopReason", ""), "stop")
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
            return from_converse_response(response, model.provider_model_id)
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
                usage = (event.get("metadata") or {}).get("usage") or {}
                if usage:
                    input_tokens = usage.get("inputTokens") or input_tokens
                    output_tokens = usage.get("outputTokens") or output_tokens
                delta, finish = converse_event_to_delta(event)
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
