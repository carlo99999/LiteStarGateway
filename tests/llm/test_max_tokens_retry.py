"""Reasoning models reject `max_tokens` and want `max_completion_tokens`. The
OpenAI-compatible adapter swaps and retries once on that specific 400, so a
stock client sending `max_tokens` still works — without affecting other models
or providers (which never raise it)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from openai import BadRequestError

from litestar_gateway.infrastructure.llm.openai_adapter import (
    _achat_create,
    _is_max_tokens_error,
    _swap_max_tokens,
)


def _max_tokens_400() -> BadRequestError:
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    return BadRequestError(
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead.",
        response=resp,
        body=None,
    )


def test_swap_and_detect() -> None:
    assert _swap_max_tokens({"max_tokens": 10}) == {"max_completion_tokens": 10}
    assert _swap_max_tokens({"max_completion_tokens": 10}) is None
    assert _swap_max_tokens({"messages": []}) is None
    assert _is_max_tokens_error(_max_tokens_400()) is True


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if "max_tokens" in kwargs:  # reasoning model rejects it the first time
            raise _max_tokens_400()
        return "ok"


class _Chat:
    def __init__(self) -> None:
        self.completions = _Completions()


class _Client:
    def __init__(self) -> None:
        self.chat = _Chat()


async def test_retries_with_max_completion_tokens() -> None:
    client = _Client()
    result = await _achat_create(client, {"model": "m", "max_tokens": 16})
    assert result == "ok"
    # First call used max_tokens (rejected); retry used max_completion_tokens.
    assert client.chat.completions.calls[0].get("max_tokens") == 16
    assert client.chat.completions.calls[1].get("max_completion_tokens") == 16
    assert "max_tokens" not in client.chat.completions.calls[1]


async def test_unrelated_400_is_not_retried() -> None:
    class _Boom(_Completions):
        async def create(self, **kwargs: Any) -> str:
            resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
            raise BadRequestError("some other problem", response=resp, body=None)

    client = _Client()
    client.chat.completions = _Boom()
    with pytest.raises(BadRequestError):
        await _achat_create(client, {"model": "m", "max_tokens": 16})
