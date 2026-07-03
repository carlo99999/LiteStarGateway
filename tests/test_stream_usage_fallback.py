"""Streamed calls must never be billed as zero when tokens were consumed.

If a client disconnects mid-stream (GeneratorExit) or the provider stream ends
without an authoritative usage chunk, the gateway falls back to estimating
usage from the request text and the streamed output (~4 chars/token) instead
of recording 0 tokens. An authoritative usage chunk always wins — estimation
never overrides or double-counts it.

Unit tests for CompletionService._metered_stream with fake ports.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent

TEAM_ID = uuid4()
KEY_ID = uuid4()

# 4-char strings estimate to exactly 1 token each (4 chars/token heuristic).
PROMPT_TEXT = "hiya"


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class FakeModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        return self._model if name == self._model.name else None


class FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"api_key": "sk-x"}  # pragma: allowlist secret


class FakeUsage:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        # The real repository's first await is a DB roundtrip — a cancellation
        # checkpoint. Mirror it so a cancelled scope re-raises here like in prod.
        await anyio.lowlevel.checkpoint()
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")


class StreamGateway:
    """Streams a fixed list of chunks for both chat and Responses operations."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    async def astream_chat_completion(self, request, model, credentials):
        return self._stream()

    async def astream_responses(self, request, model, credentials):
        return self._stream()

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk


def _service(gateway: Any, usage: FakeUsage, traces: list[TraceRecord]) -> CompletionService:
    return CompletionService(
        models=FakeModels(_model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,
        usage=usage,  # type: ignore[arg-type]
        emit_trace=traces.append,
    )


async def _disconnect(stream: AsyncIterator[dict[str, Any]]) -> None:
    """Simulate a client disconnect: GeneratorExit inside the metered generator."""
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()


def _chat_chunks() -> list[dict[str, Any]]:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "abcdefgh"}}]},  # 8 chars → 2 tokens
        {"choices": [{"index": 0, "delta": {"content": "ijkl"}}]},  # 4 chars → 1 token
    ]


CHAT_REQUEST = {"model": "m", "messages": [{"role": "user", "content": PROMPT_TEXT}]}


async def test_disconnect_mid_stream_records_estimated_usage() -> None:
    # Client disconnects after the first content chunk: bill what was seen
    # (8 streamed chars → 2 tokens) plus the estimated prompt, not zero.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(StreamGateway(_chat_chunks()), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))
    consumed = 0
    async for _ in stream:
        consumed += 1
        if consumed == 2:  # role chunk + first content chunk
            break
    await _disconnect(stream)

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 2
    assert [t.status for t in traces] == ["ok"]


async def test_disconnect_before_any_output_still_bills_prompt() -> None:
    # Disconnect right after the role chunk, before any content: the provider
    # still consumed the prompt, so the prompt estimate must be billed.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(StreamGateway(_chat_chunks()), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))
    async for _ in stream:
        break  # role chunk only — zero streamed output
    await _disconnect(stream)

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 0
    assert [t.status for t in traces] == ["ok"]


async def test_stream_without_usage_chunk_records_estimated_usage() -> None:
    # The provider stream ends cleanly but never reports usage: estimate from
    # the full streamed output (12 chars → 3 tokens) and the prompt.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(StreamGateway(_chat_chunks()), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))
    async for _ in stream:
        pass

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 3
    assert [t.status for t in traces] == ["ok"]


async def test_authoritative_usage_chunk_wins_over_estimation() -> None:
    # When the provider reports usage, bill exactly that — the streamed text
    # must not be double-counted or override it.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    chunks = [
        *_chat_chunks(),
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
    ]
    service = _service(StreamGateway(chunks), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))
    async for _ in stream:
        pass

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 5
    assert usage.events[0].completion_tokens == 7


async def test_all_zero_usage_chunk_falls_back_to_estimation() -> None:
    # A trailing usage chunk with all-zero counts (provider reported nothing)
    # is not authoritative — fall back to estimation instead of billing zero.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    chunks = [
        *_chat_chunks(),
        {
            "choices": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
    ]
    service = _service(StreamGateway(chunks), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))
    async for _ in stream:
        pass

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 3


class BlockingStreamGateway(StreamGateway):
    """Streams its chunks, then blocks forever at the provider await — the
    consumer task is left suspended inside `async for chunk in stream`, which
    is exactly where a real client disconnect delivers its cancellation."""

    def __init__(self, chunks: list[dict[str, Any]], blocked: anyio.Event) -> None:
        super().__init__(chunks)
        self._blocked = blocked

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk
        self._blocked.set()
        await anyio.sleep_forever()


async def test_cancellation_mid_stream_still_records_usage() -> None:
    # A real client disconnect cancels the streaming response's scope, raising
    # CancelledError at the provider await — unlike the benign aclose() path
    # above, the settlement then runs inside an already-cancelled scope. The
    # billing write must be shielded so the event still lands (H13).
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    blocked = anyio.Event()
    service = _service(BlockingStreamGateway(_chat_chunks(), blocked), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))

    async def consume() -> None:
        async for _ in stream:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await blocked.wait()  # all chunks streamed; consumer parked at provider await
        tg.cancel_scope.cancel()

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 3  # 12 streamed chars → 3 tokens
    assert [t.status for t in traces] == ["ok"]


async def test_responses_stream_disconnect_records_estimated_usage() -> None:
    # Same fallback for the Responses event shape: output_text deltas are
    # estimated when no usage arrives before the disconnect.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    chunks = [
        {"type": "response.created", "response": {"id": "r", "status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": "abcdefgh"},  # 8 chars → 2 tokens
        {"type": "response.output_text.delta", "delta": "ijkl"},
    ]
    service = _service(StreamGateway(chunks), usage, traces)

    stream = await service.open_responses_stream(
        TEAM_ID, KEY_ID, {"model": "m", "input": PROMPT_TEXT}
    )
    consumed = 0
    async for _ in stream:
        consumed += 1
        if consumed == 2:  # created event + first delta
            break
    await _disconnect(stream)

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 2
    assert [t.status for t in traces] == ["ok"]


class FailingMidStreamGateway(StreamGateway):
    """Streams its chunks, then dies like a provider dropping mid-stream."""

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk
        raise RuntimeError("provider died")


async def test_provider_error_mid_stream_bills_streamed_usage() -> None:
    # Tokens streamed before a provider failure were paid upstream: a provider
    # dying at 95% of a long stream must not zero out the bill. The estimate
    # machinery used for disconnects applies; the trace stays status='error'.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(FailingMidStreamGateway(_chat_chunks()), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(CHAT_REQUEST))
    with pytest.raises(RuntimeError):
        async for _ in stream:
            pass

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 3  # 12 streamed chars → 3 tokens
    assert [t.status for t in traces] == ["error"]  # no fake 'ok' trace
    assert traces[0].completion_tokens == 3  # the error trace carries the bill
