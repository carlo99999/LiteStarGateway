"""Vertex AI (Gemini) adapter: translates OpenAI chat.completions ↔ Gemini.

Pure translators (`to_gemini_request` / `from_gemini_response`) + a thin client
wrapper using the `google-genai` SDK. Responses are provided by wrapping this
adapter in `ChatToResponsesAdapter`.

Credential `values`: `vertex_project`, `vertex_location`, and (in production)
`vertex_credentials` — the service-account JSON. Without it, Application Default
Credentials are used.

Scope: text-in/text-out, plus structured outputs (`response_format`) translated
to `response_mime_type` + `response_schema`, streaming included (the same request
builder feeds both paths). Not yet translated: tools, multimodal.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai.types import HttpOptions
from google.oauth2 import service_account

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.exceptions import CredentialMisconfigured
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


def to_gemini_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = model.merge_params(request)
    ensure_translatable_chat_request(effective, model.provider.value)

    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in effective.get("messages", []):
        role = message.get("role")
        text = _text(message.get("content"))
        if role == "system":
            system_parts.append(text)
        else:  # Gemini uses "model" for the assistant role
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
            )

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

    return {"model": model.provider_model_id, "contents": contents, "config": config}


def from_gemini_response(response: dict[str, Any]) -> dict[str, Any]:
    candidate = (response.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
    usage = response.get("usage_metadata") or {}
    return {
        "id": response.get("response_id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.get("model_version"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _FINISH_REASON.get(candidate.get("finish_reason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_token_count"),
            "completion_tokens": usage.get("candidates_token_count"),
            "total_tokens": usage.get("total_token_count"),
        },
    }


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
            return from_gemini_response(response.model_dump())
        finally:
            client.close()

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = self._client(credentials)
        try:
            response = await client.aio.models.generate_content(**to_gemini_request(request, model))
            return from_gemini_response(response.model_dump())
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
