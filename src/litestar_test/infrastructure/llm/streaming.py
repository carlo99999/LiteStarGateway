"""Streaming helpers.

`mock_chat_stream` is a placeholder used by every adapter on this branch: it
yields a deterministic sequence of OpenAI `chat.completion.chunk` dicts so the
streaming protocol (endpoint → service → gateway → adapter) can be wired and
tested end-to-end. Per-provider branches replace it with real SDK streaming.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from litestar_test.domain.entities import Model

_MOCK_PIECES = ("Hello", " from", " the", " mock", " stream")


async def mock_chat_stream(model: Model) -> AsyncIterator[dict[str, Any]]:
    base = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model.provider_model_id,
    }
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    for piece in _MOCK_PIECES:
        yield {
            **base,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
    yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
