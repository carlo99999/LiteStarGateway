"""Locust workload for readiness and authenticated chat-completion capacity."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from load_test_settings import (
    LoadConfigurationError,
    LoadResponseError,
    LoadTestSettings,
    evaluate_load_gate,
    is_sse_content_line,
    iter_bounded_lines,
    validate_sse_stream,
)
from locust import FastHttpUser, LoadTestShape, constant_throughput, events, task
from locust.contrib.fasthttp import ResponseContextManager
from locust.env import Environment
from locust.runners import WorkerRunner
from locust.stats import RequestStats

try:
    SETTINGS = LoadTestSettings.from_environment()
except LoadConfigurationError as exc:
    raise SystemExit(f"load-test configuration error: {exc}") from exc

READINESS_NAME = "GET /health/ready"
CHAT_NAME = "POST /v1/chat/completions [complete]"
STREAM_NAME = "POST /v1/chat/completions [stream complete]"
TTFT_NAME = "SSE /v1/chat/completions [TTFT]"
STREAM_TTFT_STATS = RequestStats()
STEADY_STARTED_AT: float | None = None


class GatewayUser(FastHttpUser):
    """One bounded-rate caller; the shape supplies enough concurrent users."""

    host = "http://127.0.0.1:8000"
    insecure = False
    wait_time = constant_throughput(SETTINGS.per_user_rps)

    @task
    def gateway_request(self) -> None:
        if SETTINGS.mode == "readiness":
            self._readiness()
            return
        self._chat_completion(stream=SETTINGS.mode == "chat-stream")

    def _readiness(self) -> None:
        with self.client.get(
            "/health/ready",
            name=READINESS_NAME,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"unexpected readiness status {response.status_code}")

    def _chat_completion(self, *, stream: bool) -> None:
        headers = {"Authorization": f"Bearer {SETTINGS.api_key}"}
        payload: dict[str, Any] = {
            "model": SETTINGS.model,
            "messages": [{"role": "user", "content": SETTINGS.prompt}],
            "max_tokens": SETTINGS.max_tokens,
            "stream": stream,
        }
        name = STREAM_NAME if stream else CHAT_NAME
        started = time.perf_counter()
        with self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers=headers,
            name=name,
            catch_response=True,
            stream=stream,
            allow_redirects=False,
        ) as response:
            if response.status_code == 429:
                response.failure(
                    "unexpected 429; raise INFERENCE_RATE_LIMIT_RPM only in the load profile"
                )
                return
            if response.status_code != 200:
                response.failure(f"unexpected chat status {response.status_code}")
                return
            if stream:
                self._validate_stream(response, started=started)
                return
            try:
                body = response.json()
            except ValueError:
                response.failure("chat response is not valid JSON")
                return
            choices = body.get("choices") if isinstance(body, dict) else None
            if not isinstance(choices, list) or not choices:
                response.failure("chat response has no choices")

    def _validate_stream(
        self,
        response: ResponseContextManager,
        *,
        started: float,
    ) -> None:
        ttft_recorded = False

        def measured_lines() -> Iterator[str | bytes]:
            nonlocal ttft_recorded
            chunks = response.iter_content(chunk_size=1, decode_content=True)
            for line in iter_bounded_lines(chunks, max_bytes=SETTINGS.max_stream_bytes):
                if (
                    not ttft_recorded
                    and STEADY_STARTED_AT is not None
                    and is_sse_content_line(line)
                ):
                    ttft_ms = (time.perf_counter() - started) * 1000
                    STREAM_TTFT_STATS.log_request("SSE", TTFT_NAME, int(ttft_ms), 0)
                    ttft_recorded = True
                yield line

        try:
            validate_sse_stream(measured_lines())
        except LoadResponseError as exc:
            response.failure(str(exc))
        finally:
            response.request_meta["response_time"] = (time.perf_counter() - started) * 1000


class GatewayLoadShape(LoadTestShape):
    """Ramp, settle in-flight work, then measure the steady-state window."""

    _steady_state_started = False

    def tick(self) -> tuple[int, float] | None:
        elapsed = self.get_run_time()
        if elapsed >= SETTINGS.total_duration_seconds:
            return None

        if SETTINGS.ramp_seconds and elapsed < SETTINGS.ramp_seconds:
            fraction = elapsed / SETTINGS.ramp_seconds
            users = max(1, round(SETTINGS.user_count * fraction))
            return users, SETTINGS.spawn_rate

        steady_start = SETTINGS.ramp_seconds + SETTINGS.settle_seconds
        if elapsed < steady_start:
            return SETTINGS.user_count, SETTINGS.spawn_rate

        if not self._steady_state_started and self.runner is not None:
            global STEADY_STARTED_AT
            self.runner.stats.reset_all()
            STREAM_TTFT_STATS.reset_all()
            STEADY_STARTED_AT = time.perf_counter()
            self._steady_state_started = True
        return SETTINGS.user_count, SETTINGS.spawn_rate


@events.test_start.add_listener
def report_configuration(environment: Environment, **_: object) -> None:
    global STEADY_STARTED_AT
    if isinstance(environment.runner, WorkerRunner):
        return
    STREAM_TTFT_STATS.reset_all()
    STEADY_STARTED_AT = None
    print(
        "Load profile:"
        f" mode={SETTINGS.mode}"
        f" target={SETTINGS.target_rps:g} RPS"
        f" users={SETTINGS.user_count}"
        f" expected_latency={SETTINGS.expected_latency_seconds:g}s"
        f" steady={SETTINGS.duration_seconds:g}s"
        f" warmup={SETTINGS.ramp_seconds + SETTINGS.settle_seconds:g}s"
    )


def _primary_stats(environment: Environment) -> Any:
    if SETTINGS.mode == "readiness":
        return environment.stats.get(READINESS_NAME, "GET")
    name = STREAM_NAME if SETTINGS.mode == "chat-stream" else CHAT_NAME
    return environment.stats.get(name, "POST")


@events.quitting.add_listener
def enforce_service_levels(environment: Environment, **_: object) -> None:
    if isinstance(environment.runner, WorkerRunner):
        return

    primary = _primary_stats(environment)
    elapsed = (
        max(time.perf_counter() - STEADY_STARTED_AT, 0.001)
        if STEADY_STARTED_AT is not None
        else 0.001
    )
    successful_rps = (primary.num_requests - primary.num_failures) / elapsed
    failure_ratio = primary.fail_ratio
    p95_ms = primary.get_response_time_percentile(0.95) or 0.0

    ttft_ms = 0.0
    ttft_samples = 0
    if SETTINGS.mode == "chat-stream":
        ttft = STREAM_TTFT_STATS.get(TTFT_NAME, "SSE")
        ttft_ms = ttft.get_response_time_percentile(0.95) or 0.0
        ttft_samples = ttft.num_requests

    gate = evaluate_load_gate(
        SETTINGS,
        num_requests=primary.num_requests,
        successful_rps=successful_rps,
        failure_ratio=failure_ratio,
        p95_ms=p95_ms,
        ttft_samples=ttft_samples,
        ttft_p95_ms=ttft_ms,
    )

    outcome = "PASS" if gate.passed else "FAIL"
    print(
        f"Load gate {outcome}: achieved={successful_rps:.1f} successful RPS"
        f" failures={failure_ratio:.3%} p95={p95_ms:.0f}ms"
        f" ttft_p95={ttft_ms:.0f}ms"
    )
    if gate.failures:
        for failure in gate.failures:
            print(f"  - {failure}")
    environment.process_exit_code = 0 if gate.passed else 1
