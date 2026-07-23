"""Validated, secret-safe settings shared by the Locust load runner."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

LoadMode = Literal["readiness", "chat", "chat-stream"]
StreamData = str | bytes | bytearray


class LoadConfigurationError(ValueError):
    """Raised before a load run when its environment is unsafe or incomplete."""


class LoadResponseError(ValueError):
    """Raised when a load response is incomplete or malformed."""


@dataclass(frozen=True)
class StreamSummary:
    """Minimal, non-sensitive facts retained from one SSE response."""

    chunk_count: int
    content_chunk_count: int


def _decode_stream_data(value: StreamData) -> str:
    if isinstance(value, str):
        return value
    return bytes(value).decode("utf-8", errors="replace")


def iter_bounded_lines(
    chunks: Iterable[StreamData],
    *,
    max_bytes: int,
) -> Iterable[str]:
    """Split streaming chunks into lines while enforcing a total byte bound."""

    buffer = ""
    consumed_bytes = 0
    for chunk in chunks:
        consumed_bytes += len(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        if consumed_bytes > max_bytes:
            raise LoadResponseError("stream exceeds the configured byte limit")
        text = _decode_stream_data(chunk)
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer:
        yield buffer


def is_sse_content_line(raw_line: StreamData) -> bool:
    """Return true only for a valid SSE chunk containing output content."""

    line = _decode_stream_data(raw_line)
    line = line.strip()
    if not line.startswith("data:"):
        return False
    data = line.removeprefix("data:").strip()
    if data == "[DONE]":
        return False
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict):
        return False
    choices = event.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, dict)
        and isinstance(choice.get("delta"), dict)
        and isinstance(choice["delta"].get("content"), str)
        and bool(choice["delta"]["content"])
        for choice in choices
    )


def parse_progressive_targets(raw: str) -> tuple[float, ...]:
    """Parse a positive, strictly increasing comma-separated RPS profile."""

    parts = raw.split(",")
    if not raw.strip() or any(not part.strip() for part in parts):
        raise LoadConfigurationError("LOAD_STAGES must contain comma-separated RPS values")

    targets: list[float] = []
    for part in parts:
        try:
            target = float(part)
        except ValueError as exc:
            raise LoadConfigurationError("LOAD_STAGES values must be numbers") from exc
        if not math.isfinite(target) or target <= 0:
            raise LoadConfigurationError("LOAD_STAGES values must be greater than zero")
        targets.append(target)

    if any(current <= previous for previous, current in zip(targets, targets[1:], strict=False)):
        raise LoadConfigurationError("LOAD_STAGES values must be strictly increasing")
    return tuple(targets)


def validate_sse_stream(lines: Iterable[StreamData]) -> StreamSummary:
    """Consume an OpenAI-compatible SSE stream without retaining generated text."""

    chunk_count = 0
    content_chunk_count = 0
    done = False

    for raw_line in lines:
        line = _decode_stream_data(raw_line)
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue

        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            done = True
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LoadResponseError("stream contains invalid JSON") from exc
        if not isinstance(event, dict):
            raise LoadResponseError("stream JSON chunk must be an object")
        if "error" in event:
            raise LoadResponseError("stream contains an error event")

        chunk_count += 1
        choices = event.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str) and content:
                content_chunk_count += 1

    if not done:
        raise LoadResponseError("stream is missing the [DONE] marker")
    if chunk_count == 0:
        raise LoadResponseError("stream contains no JSON chunks")
    if content_chunk_count == 0:
        raise LoadResponseError("stream contains no output content")
    return StreamSummary(
        chunk_count=chunk_count,
        content_chunk_count=content_chunk_count,
    )


def _float(
    environment: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    raw = environment.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise LoadConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise LoadConfigurationError(f"{name} must be >= {minimum}{upper}")
    return value


@dataclass(frozen=True)
class LoadTestSettings:
    """Runtime inputs for one readiness or authenticated chat load test."""

    mode: LoadMode
    target_rps: float
    expected_latency_seconds: float
    user_headroom: float
    duration_seconds: float
    ramp_seconds: float
    settle_seconds: float
    max_failure_ratio: float
    min_rps_ratio: float
    max_p95_ms: float
    max_ttft_ms: float
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None
    prompt: str = "Reply with the single word OK."
    max_tokens: int = 8
    max_stream_bytes: int = 1_000_000

    @property
    def user_count(self) -> int:
        return max(
            1,
            math.ceil(self.target_rps * self.expected_latency_seconds * self.user_headroom),
        )

    @property
    def per_user_rps(self) -> float:
        return self.target_rps / self.user_count

    @property
    def spawn_rate(self) -> float:
        ramp = max(self.ramp_seconds, 1.0)
        return max(1.0, self.user_count / ramp)

    @property
    def total_duration_seconds(self) -> float:
        return self.ramp_seconds + self.settle_seconds + self.duration_seconds

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> LoadTestSettings:
        source = os.environ if environment is None else environment
        mode = source.get("LOAD_MODE", "readiness").strip().lower()
        if mode not in {"readiness", "chat", "chat-stream"}:
            raise LoadConfigurationError("LOAD_MODE must be 'readiness', 'chat', or 'chat-stream'")

        api_key = source.get("LOAD_API_KEY") or None
        model = source.get("LOAD_MODEL") or None
        if mode in {"chat", "chat-stream"} and not api_key:
            raise LoadConfigurationError("LOAD_API_KEY is required in chat modes")
        if mode in {"chat", "chat-stream"} and not model:
            raise LoadConfigurationError("LOAD_MODEL is required in chat modes")

        target_rps = _float(source, "LOAD_TARGET_RPS", 10.0, minimum=0.001)
        expected_latency = _float(
            source,
            "LOAD_EXPECTED_LATENCY_SECONDS",
            0.1,
            minimum=0.001,
        )
        headroom = _float(source, "LOAD_USER_HEADROOM", 1.25, minimum=1.0)
        duration = _float(source, "LOAD_DURATION_SECONDS", 60.0, minimum=1.0)
        ramp = _float(source, "LOAD_RAMP_SECONDS", 10.0, minimum=0.0)
        settle = _float(source, "LOAD_SETTLE_SECONDS", 5.0, minimum=0.0)

        raw_max_tokens = source.get("LOAD_MAX_TOKENS", "8")
        try:
            max_tokens = int(raw_max_tokens)
        except ValueError as exc:
            raise LoadConfigurationError("LOAD_MAX_TOKENS must be an integer") from exc
        if max_tokens < 1 or max_tokens > 4096:
            raise LoadConfigurationError("LOAD_MAX_TOKENS must be between 1 and 4096")

        raw_max_stream_bytes = source.get("LOAD_MAX_STREAM_BYTES", "1000000")
        try:
            max_stream_bytes = int(raw_max_stream_bytes)
        except ValueError as exc:
            raise LoadConfigurationError("LOAD_MAX_STREAM_BYTES must be an integer") from exc
        if max_stream_bytes < 1024 or max_stream_bytes > 10_000_000:
            raise LoadConfigurationError("LOAD_MAX_STREAM_BYTES must be between 1024 and 10000000")

        prompt = source.get("LOAD_PROMPT", "Reply with the single word OK.")
        if not prompt or len(prompt) > 10_000:
            raise LoadConfigurationError("LOAD_PROMPT must contain 1 to 10000 characters")

        return cls(
            mode=mode,
            target_rps=target_rps,
            expected_latency_seconds=expected_latency,
            user_headroom=headroom,
            duration_seconds=duration,
            ramp_seconds=ramp,
            settle_seconds=settle,
            max_failure_ratio=_float(
                source,
                "LOAD_MAX_FAILURE_RATIO",
                0.001,
                minimum=0.0,
                maximum=1.0,
            ),
            min_rps_ratio=_float(
                source,
                "LOAD_MIN_RPS_RATIO",
                0.95,
                minimum=0.0,
                maximum=1.0,
            ),
            max_p95_ms=_float(source, "LOAD_MAX_P95_MS", 1000.0, minimum=1.0),
            max_ttft_ms=_float(source, "LOAD_MAX_TTFT_MS", 2000.0, minimum=1.0),
            api_key=api_key,
            model=model,
            prompt=prompt,
            max_tokens=max_tokens,
            max_stream_bytes=max_stream_bytes,
        )
