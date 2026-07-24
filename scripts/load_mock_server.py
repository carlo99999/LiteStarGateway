"""Deterministic OpenAI-compatible upstream used by the benchmark contract."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class MockConfigurationError(ValueError):
    """Raised when deterministic mock timing or failure settings are invalid."""


def _number(
    environment: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    try:
        value = float(environment.get(name, str(default)))
    except ValueError as exc:
        raise MockConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise MockConfigurationError(f"{name} must be >= {minimum}")
    return value


def _integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as exc:
        raise MockConfigurationError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise MockConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class MockSettings:
    """Validated deterministic behavior for the mock provider."""

    ttft_ms: float
    chunk_interval_ms: float
    total_latency_ms: float
    chunk_count: int
    failure_every: int
    failure_status: int

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> MockSettings:
        source = os.environ if environment is None else environment
        settings = cls(
            ttft_ms=_number(source, "LOAD_MOCK_TTFT_MS", 25, minimum=0),
            chunk_interval_ms=_number(
                source,
                "LOAD_MOCK_CHUNK_INTERVAL_MS",
                10,
                minimum=0,
            ),
            total_latency_ms=_number(
                source,
                "LOAD_MOCK_TOTAL_LATENCY_MS",
                50,
                minimum=0,
            ),
            chunk_count=_integer(
                source,
                "LOAD_MOCK_CHUNK_COUNT",
                2,
                minimum=1,
                maximum=100,
            ),
            failure_every=_integer(
                source,
                "LOAD_MOCK_FAILURE_EVERY",
                0,
                minimum=0,
                maximum=1_000_000,
            ),
            failure_status=_integer(
                source,
                "LOAD_MOCK_FAILURE_STATUS",
                503,
                minimum=400,
                maximum=599,
            ),
        )
        minimum_total = settings.ttft_ms + (settings.chunk_interval_ms * (settings.chunk_count - 1))
        if settings.total_latency_ms < minimum_total:
            raise MockConfigurationError(
                "LOAD_MOCK_TOTAL_LATENCY_MS must cover TTFT and all chunk intervals"
            )
        return settings


@dataclass
class _Counters:
    requests: int = 0
    failures: int = 0
    in_flight: int = 0
    max_in_flight: int = 0

    def begin(self) -> int:
        self.requests += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        return self.requests

    def finish(self, *, failed: bool) -> None:
        self.failures += int(failed)
        self.in_flight -= 1

    def snapshot(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "in_flight": self.in_flight,
            "max_in_flight": self.max_in_flight,
        }


def _headers(content_type: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", content_type),
        (b"cache-control", b"no-store"),
    ]


async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
    encoded = json.dumps(body, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": _headers(b"application/json"),
        }
    )
    await send({"type": "http.response.body", "body": encoded})


async def _read_json(receive: Any, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    chunks: list[bytes] = []
    size = 0
    more = True
    while more:
        message = await receive()
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("request body is too large")
        chunks.append(chunk)
        more = bool(message.get("more_body"))
    try:
        value = json.loads(b"".join(chunks) or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("request body is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _completion(model: str, request_id: str, created: int) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _content_parts(count: int) -> tuple[str, ...]:
    if count == 1:
        return ("OK",)
    return ("O", "K", *("" for _ in range(count - 2)))


async def _send_stream(
    send: Any,
    settings: MockSettings,
    *,
    model: str,
    request_id: str,
    created: int,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": _headers(b"text/event-stream"),
        }
    )
    started = time.perf_counter()
    if settings.ttft_ms:
        await asyncio.sleep(settings.ttft_ms / 1000)
    parts = _content_parts(settings.chunk_count)
    for index, content in enumerate(parts):
        chunk = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content},
                    "finish_reason": None,
                }
            ],
        }
        await send(
            {
                "type": "http.response.body",
                "body": f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode(),
                "more_body": True,
            }
        )
        if index < len(parts) - 1 and settings.chunk_interval_ms:
            await asyncio.sleep(settings.chunk_interval_ms / 1000)
    remaining = (settings.total_latency_ms / 1000) - (time.perf_counter() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)
    usage = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    body = f"data: {json.dumps(usage, separators=(',', ':'))}\n\ndata: [DONE]\n\n".encode()
    await send({"type": "http.response.body", "body": body, "more_body": False})


def create_mock_app(settings: MockSettings) -> Any:
    """Create a dependency-free ASGI app with process-local deterministic counters."""

    counters = _Counters()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        path = scope.get("path")
        method = scope.get("method")
        if method == "GET" and path == "/health":
            await _send_json(send, 200, {"status": "ok"})
            return
        if method == "GET" and path == "/metrics":
            await _send_json(send, 200, counters.snapshot())
            return
        if method != "POST" or path != "/v1/chat/completions":
            await _send_json(send, 404, {"error": {"type": "not_found"}})
            return

        failed = False
        ordinal = counters.begin()
        try:
            try:
                body = await _read_json(receive)
            except ValueError as exc:
                failed = True
                await _send_json(
                    send,
                    400,
                    {"error": {"type": "invalid_request", "message": str(exc)}},
                )
                return
            if settings.failure_every and ordinal % settings.failure_every == 0:
                failed = True
                await _send_json(
                    send,
                    settings.failure_status,
                    {"error": {"type": "mock_injected_failure", "message": "injected failure"}},
                )
                return

            model = body.get("model")
            if not isinstance(model, str) or not model:
                failed = True
                await _send_json(
                    send,
                    400,
                    {"error": {"type": "invalid_request", "message": "model is required"}},
                )
                return
            request_id = f"chatcmpl-mock-{uuid.uuid4().hex}"
            created = int(time.time())
            if body.get("stream") is True:
                await _send_stream(
                    send,
                    settings,
                    model=model,
                    request_id=request_id,
                    created=created,
                )
                return
            if settings.total_latency_ms:
                await asyncio.sleep(settings.total_latency_ms / 1000)
            await _send_json(send, 200, _completion(model, request_id, created))
        finally:
            counters.finish(failed=failed)

    return app


def main() -> None:
    """Run the mock under Uvicorn inside the benchmark stack."""

    import uvicorn

    uvicorn.run(create_mock_app(MockSettings.from_environment()), host="0.0.0.0", port=9000)


if __name__ == "__main__":
    main()
