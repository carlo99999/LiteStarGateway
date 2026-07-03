"""Emulate the Responses API on top of chat.completions.

For providers without a native Responses API (e.g. Databricks), we translate a
Responses request into a chat.completions call and reshape the chat response
into a Responses object. Pure translators + a thin wrapper adapter.

The client keeps using the stock OpenAI SDK (`client.responses.create`), so it
"just works" — but emulation only covers the common text-in/text-out path.
NOT supported (the corresponding request fields are ignored):
  * tools / function calling
  * structured outputs (`response_format` / json schema)
  * multimodal input (images, files, audio)
  * stateful conversations (`previous_response_id`, `store`)
  * reasoning items / built-in tools (web search, file search, ...)
Natively-supported providers (OpenAI, Azure) get the full Responses feature set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from litestar_gateway.domain.entities import Model

# Chat sampling params we carry over verbatim from a Responses request.
_PASSTHROUGH = frozenset(
    {"temperature", "top_p", "stop", "presence_penalty", "frequency_penalty", "seed", "n"}
)


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


def _input_to_messages(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    messages = []
    for item in value:
        if isinstance(item, dict):
            messages.append(
                {"role": item.get("role", "user"), "content": _extract_text(item.get("content"))}
            )
    return messages


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
    return chat


def to_responses(chat_response: dict[str, Any]) -> dict[str, Any]:
    """chat.completions response -> Responses-shaped response."""
    choice = (chat_response.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = chat_response.get("usage") or {}
    return {
        "id": chat_response.get("id"),
        "object": "response",
        "created_at": chat_response.get("created"),
        "model": chat_response.get("model"),
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
    }


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
