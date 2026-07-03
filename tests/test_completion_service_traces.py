"""Failed LLM calls must emit a status='error' trace (M14).

Unit tests for CompletionService with fake ports: a gateway failure — on the
plain path or mid-stream — produces a TraceRecord(status="error") with the
exception type; success still records usage + an 'ok' trace.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent

TEAM_ID = uuid4()
KEY_ID = uuid4()


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
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")


class FailingGateway:
    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        raise RuntimeError("provider exploded")

    async def astream_chat_completion(self, request, model, credentials):
        async def broken() -> AsyncIterator[dict[str, Any]]:
            yield {"choices": [{"delta": {"content": "hi"}}]}
            raise RuntimeError("mid-stream failure")

        return broken()


class OkGateway:
    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        return {"usage": {"prompt_tokens": 2, "completion_tokens": 3}}


def _service(gateway: Any, usage: FakeUsage, traces: list[TraceRecord]) -> CompletionService:
    return CompletionService(
        models=FakeModels(_model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,
        usage=usage,  # type: ignore[arg-type]
        emit_trace=traces.append,
    )


async def test_failed_call_emits_error_trace() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(FailingGateway(), usage, traces)

    with pytest.raises(RuntimeError):
        await service.chat_completion(TEAM_ID, KEY_ID, {"model": "m", "messages": []})

    assert len(traces) == 1
    trace = traces[0]
    assert trace.status == "error"
    assert trace.error_type == "RuntimeError"
    assert trace.operation == "chat.completions"
    assert (trace.prompt_tokens, trace.completion_tokens, trace.cost) == (0, 0, 0.0)
    assert trace.latency_ms >= 0
    assert usage.events == []  # no fake usage row for a failed call


async def test_midstream_failure_emits_error_trace_not_ok() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(FailingGateway(), usage, traces)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, {"model": "m", "messages": []})
    with pytest.raises(RuntimeError):
        async for _ in stream:
            pass

    assert [t.status for t in traces] == ["error"]
    assert traces[0].error_type == "RuntimeError"
    # The 2 chars streamed before the failure were paid upstream: billed as an
    # estimate (1 token), while the trace honestly stays 'error' and carries it.
    assert len(usage.events) == 1
    assert usage.events[0].completion_tokens == 1
    assert traces[0].completion_tokens == 1


async def test_successful_call_still_records_ok_trace_and_usage() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    service = _service(OkGateway(), usage, traces)

    await service.chat_completion(TEAM_ID, KEY_ID, {"model": "m", "messages": []})

    assert [t.status for t in traces] == ["ok"]
    assert traces[0].error_type is None
    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 2
