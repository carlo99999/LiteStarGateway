"""Vertex AI (Gemini) adapter: translates OpenAI chat.completions ↔ Gemini.

Pure translators (`to_gemini_request` / `from_gemini_response`) + a thin client
wrapper using the `google-genai` SDK. Responses are provided by wrapping this
adapter in `ChatToResponsesAdapter`.

Credential `values`: `vertex_project`, `vertex_location`, and (in production)
`vertex_credentials` — the service-account JSON. Without it, Application Default
Credentials are used.

First-cut scope: text-in/text-out. Not yet translated: tools, multimodal,
structured outputs.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.oauth2 import service_account

from litestar_test.domain.entities import Model

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
    effective = {**model.params, **request}

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
    effective = {**model.params, **request}
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
    return {
        "object": "list",
        "data": data,
        "model": model_id,
        "usage": {"prompt_tokens": None, "total_tokens": None},
    }


def to_imagen_request(request: dict[str, Any], model: Model) -> dict[str, Any]:
    effective = {**model.params, **request}
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


def _client(credentials: dict[str, str]) -> genai.Client:
    creds = None
    if raw := credentials.get("vertex_credentials"):
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=[_SCOPE]
        )
    return genai.Client(
        vertexai=True,
        project=credentials.get("vertex_project"),
        location=credentials.get("vertex_location"),
        credentials=creds,
    )


class VertexAdapter:
    def chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = _client(credentials)
        response = client.models.generate_content(**to_gemini_request(request, model))
        return from_gemini_response(response.model_dump())

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = _client(credentials)
        response = await client.aio.models.generate_content(**to_gemini_request(request, model))
        return from_gemini_response(response.model_dump())

    async def astream_chat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        client = _client(credentials)
        base = {
            "id": "chatcmpl-gemini",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model.provider_model_id,
        }
        # Gemini has no separate "start" event; emit the role delta ourselves.
        yield {
            **base,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        stream: Any = await client.aio.models.generate_content_stream(
            **to_gemini_request(request, model)
        )
        async for chunk in stream:
            delta, finish = gemini_chunk_to_delta(chunk.model_dump())
            if delta is None and finish is None:
                continue
            yield {
                **base,
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
            }

    def embeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = _client(credentials)
        response = client.models.embed_content(**to_gemini_embed_request(request, model))
        return from_gemini_embeddings(response.model_dump(), model.provider_model_id)

    async def aembeddings(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = _client(credentials)
        response = await client.aio.models.embed_content(**to_gemini_embed_request(request, model))
        return from_gemini_embeddings(response.model_dump(), model.provider_model_id)

    def images(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = _client(credentials)
        response = client.models.generate_images(**to_imagen_request(request, model))
        return from_imagen_response(response.model_dump())

    async def aimages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        client = _client(credentials)
        response = await client.aio.models.generate_images(**to_imagen_request(request, model))
        return from_imagen_response(response.model_dump())
