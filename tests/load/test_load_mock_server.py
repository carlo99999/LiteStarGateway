from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
load_mock_server = importlib.import_module("load_mock_server")
MockConfigurationError = load_mock_server.MockConfigurationError
MockSettings = load_mock_server.MockSettings
create_mock_app = load_mock_server.create_mock_app


async def _request(
    app: Any,
    *,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    messages = [
        {
            "type": "http.request",
            "body": json.dumps(body or {}).encode(),
            "more_body": False,
        }
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "POST" if body is not None else "GET",
            "path": path,
            "headers": [],
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return start["status"], start["headers"], response_body


def _settings(**overrides: float | int) -> Any:
    values = {
        "ttft_ms": 0.0,
        "chunk_interval_ms": 0.0,
        "total_latency_ms": 0.0,
        "chunk_count": 2,
        "failure_every": 0,
        "failure_status": 503,
    }
    values.update(overrides)
    return MockSettings(**values)


@pytest.mark.asyncio
async def test_mock_returns_openai_chat_completion_with_usage() -> None:
    app = create_mock_app(_settings())

    status, headers, raw_body = await _request(
        app,
        path="/v1/chat/completions",
        body={"model": "benchmark-model", "messages": [], "stream": False},
    )

    assert status == 200
    assert (b"content-type", b"application/json") in headers
    body = json.loads(raw_body)
    assert body["object"] == "chat.completion"
    assert body["model"] == "benchmark-model"
    assert body["choices"][0]["message"]["content"] == "OK"
    assert body["usage"] == {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }


@pytest.mark.asyncio
async def test_mock_health_unknown_path_and_missing_model_are_bounded() -> None:
    app = create_mock_app(_settings())

    health, _, health_body = await _request(app, path="/health")
    missing, _, _ = await _request(app, path="/unknown")
    invalid, _, invalid_body = await _request(
        app,
        path="/v1/chat/completions",
        body={"messages": []},
    )

    assert health == 200
    assert json.loads(health_body) == {"status": "ok"}
    assert missing == 404
    assert invalid == 400
    assert json.loads(invalid_body)["error"]["type"] == "invalid_request"


@pytest.mark.asyncio
async def test_mock_stream_has_content_usage_and_done_marker() -> None:
    app = create_mock_app(_settings())

    status, headers, raw_body = await _request(
        app,
        path="/v1/chat/completions",
        body={"model": "benchmark-model", "messages": [], "stream": True},
    )

    assert status == 200
    assert (b"content-type", b"text/event-stream") in headers
    events = [
        line.removeprefix("data: ")
        for line in raw_body.decode().splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert (
        "".join(
            choice["delta"].get("content", "")
            for chunk in chunks
            for choice in chunk.get("choices", [])
        )
        == "OK"
    )
    assert chunks[-1]["usage"]["total_tokens"] == 2


@pytest.mark.asyncio
async def test_mock_applies_nonzero_stream_and_total_latency() -> None:
    app = create_mock_app(
        _settings(
            ttft_ms=1,
            chunk_interval_ms=1,
            total_latency_ms=3,
            chunk_count=2,
        )
    )

    status, _, raw_body = await _request(
        app,
        path="/v1/chat/completions",
        body={"model": "benchmark-model", "messages": [], "stream": True},
    )

    assert status == 200
    assert raw_body.endswith(b"data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_mock_failure_injection_is_deterministic_and_observable() -> None:
    app = create_mock_app(_settings(failure_every=2, failure_status=502))
    request_body = {"model": "benchmark-model", "messages": [], "stream": False}

    first, _, _ = await _request(app, path="/v1/chat/completions", body=request_body)
    second, _, second_body = await _request(app, path="/v1/chat/completions", body=request_body)
    metrics_status, _, metrics_body = await _request(app, path="/metrics")

    assert first == 200
    assert second == 502
    assert json.loads(second_body)["error"]["type"] == "mock_injected_failure"
    assert metrics_status == 200
    assert json.loads(metrics_body) == {
        "requests": 2,
        "failures": 1,
        "in_flight": 0,
        "max_in_flight": 1,
    }


@pytest.mark.parametrize(
    "environment",
    [
        {"LOAD_MOCK_TTFT_MS": "-1"},
        {"LOAD_MOCK_TOTAL_LATENCY_MS": "not-a-number"},
        {"LOAD_MOCK_CHUNK_COUNT": "0"},
        {"LOAD_MOCK_FAILURE_EVERY": "-1"},
        {"LOAD_MOCK_FAILURE_STATUS": "200"},
        {"LOAD_MOCK_FAILURE_STATUS": "not-an-integer"},
        {
            "LOAD_MOCK_TTFT_MS": "50",
            "LOAD_MOCK_TOTAL_LATENCY_MS": "40",
        },
    ],
)
def test_mock_configuration_rejects_invalid_or_inconsistent_values(
    environment: dict[str, str],
) -> None:
    with pytest.raises(MockConfigurationError):
        MockSettings.from_environment(environment)
