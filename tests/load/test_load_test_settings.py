from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
load_test_settings = importlib.import_module("load_test_settings")
run_load_profile = importlib.import_module("run_load_profile")
LoadConfigurationError = load_test_settings.LoadConfigurationError
LoadResponseError = load_test_settings.LoadResponseError
LoadTestSettings = load_test_settings.LoadTestSettings
evaluate_load_gate = load_test_settings.evaluate_load_gate
parse_progressive_targets = load_test_settings.parse_progressive_targets
validate_sse_stream = load_test_settings.validate_sse_stream
build_locust_command = run_load_profile.build_locust_command
build_stage_environment = run_load_profile.build_stage_environment
estimate_provider_budget = run_load_profile.estimate_provider_budget
execute_stages = run_load_profile.execute_stages
parse_profile_modes = run_load_profile.parse_profile_modes
parse_profile_policy = run_load_profile.parse_profile_policy
validate_load_host = run_load_profile.validate_load_host
FAKE_API_KEY = "not-a-real-load-test-key"  # pragma: allowlist secret


def _gate_settings(mode: str = "chat") -> LoadTestSettings:
    return LoadTestSettings.from_environment(
        {
            "LOAD_MODE": mode,
            "LOAD_API_KEY": FAKE_API_KEY,
            "LOAD_MODEL": "configured-model",
            "LOAD_TARGET_RPS": "100",
            "LOAD_MAX_FAILURE_RATIO": "0.01",
            "LOAD_MIN_RPS_RATIO": "0.95",
            "LOAD_MAX_P95_MS": "500",
            "LOAD_MAX_TTFT_MS": "250",
        }
    )


def test_load_gate_accepts_metrics_exactly_on_configured_thresholds() -> None:
    result = evaluate_load_gate(
        _gate_settings("chat-stream"),
        num_requests=100,
        successful_rps=95,
        failure_ratio=0.01,
        p95_ms=500,
        ttft_samples=99,
        ttft_p95_ms=250,
    )

    assert result.passed
    assert result.failures == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"successful_rps": 94.9}, "below"),
        ({"failure_ratio": 0.0101}, "failure ratio"),
        ({"p95_ms": 501}, "p95"),
        ({"ttft_p95_ms": 251}, "TTFT"),
        ({"ttft_samples": 0}, "no TTFT"),
    ],
)
def test_load_gate_rejects_metrics_beyond_thresholds(
    overrides: dict[str, float | int],
    message: str,
) -> None:
    metrics: dict[str, float | int] = {
        "num_requests": 100,
        "successful_rps": 95,
        "failure_ratio": 0.01,
        "p95_ms": 500,
        "ttft_samples": 99,
        "ttft_p95_ms": 250,
    }
    metrics.update(overrides)

    result = evaluate_load_gate(_gate_settings("chat-stream"), **metrics)

    assert not result.passed
    assert any(message in failure for failure in result.failures)


def test_chat_settings_size_users_from_rps_latency_and_headroom() -> None:
    settings = LoadTestSettings.from_environment(
        {
            "LOAD_MODE": "chat",
            "LOAD_API_KEY": FAKE_API_KEY,
            "LOAD_MODEL": "configured-model",
            "LOAD_TARGET_RPS": "300",
            "LOAD_EXPECTED_LATENCY_SECONDS": "0.25",
            "LOAD_USER_HEADROOM": "1.25",
        }
    )

    assert settings.mode == "chat"
    assert settings.target_rps == 300
    assert settings.user_count == math.ceil(300 * 0.25 * 1.25)
    assert settings.per_user_rps == pytest.approx(300 / settings.user_count)
    assert settings.total_duration_seconds == 75
    assert FAKE_API_KEY not in repr(settings)


def test_streaming_chat_uses_the_same_authenticated_capacity_sizing() -> None:
    settings = LoadTestSettings.from_environment(
        {
            "LOAD_MODE": "chat-stream",
            "LOAD_API_KEY": FAKE_API_KEY,
            "LOAD_MODEL": "configured-model",
            "LOAD_TARGET_RPS": "300",
            "LOAD_EXPECTED_LATENCY_SECONDS": "2",
            "LOAD_MAX_TTFT_MS": "1500",
        }
    )

    assert settings.mode == "chat-stream"
    assert settings.user_count == math.ceil(300 * 2 * 1.25)
    assert settings.max_ttft_ms == 1500


def test_readiness_settings_need_no_api_key() -> None:
    settings = LoadTestSettings.from_environment(
        {
            "LOAD_MODE": "readiness",
            "LOAD_TARGET_RPS": "10",
            "LOAD_EXPECTED_LATENCY_SECONDS": "0.1",
        }
    )

    assert settings.api_key is None
    assert settings.model is None
    assert settings.user_count >= 1


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOAD_TARGET_RPS", "0"),
        ("LOAD_EXPECTED_LATENCY_SECONDS", "-1"),
        ("LOAD_USER_HEADROOM", "0.5"),
        ("LOAD_DURATION_SECONDS", "0"),
        ("LOAD_RAMP_SECONDS", "-1"),
        ("LOAD_SETTLE_SECONDS", "-1"),
        ("LOAD_MAX_FAILURE_RATIO", "1.1"),
        ("LOAD_MIN_RPS_RATIO", "-0.1"),
        ("LOAD_MAX_TTFT_MS", "0"),
        ("LOAD_MAX_STREAM_BYTES", "1"),
    ],
)
def test_invalid_numeric_settings_fail_fast(name: str, value: str) -> None:
    environment = {
        "LOAD_MODE": "readiness",
        name: value,
    }

    with pytest.raises(LoadConfigurationError):
        LoadTestSettings.from_environment(environment)


@pytest.mark.parametrize(
    "environment",
    [
        {"LOAD_MODE": "chat", "LOAD_MODEL": "configured-model"},
        {"LOAD_MODE": "chat", "LOAD_API_KEY": FAKE_API_KEY},
        {"LOAD_MODE": "chat-stream", "LOAD_MODEL": "configured-model"},
        {"LOAD_MODE": "chat-stream", "LOAD_API_KEY": FAKE_API_KEY},
        {"LOAD_MODE": "unknown"},
    ],
)
def test_chat_credentials_and_mode_are_validated(environment: dict[str, str]) -> None:
    with pytest.raises(LoadConfigurationError):
        LoadTestSettings.from_environment(environment)


def test_progressive_targets_are_strictly_increasing() -> None:
    assert parse_progressive_targets("25, 50,100,200,300") == (
        25.0,
        50.0,
        100.0,
        200.0,
        300.0,
    )

    with pytest.raises(LoadConfigurationError, match="strictly increasing"):
        parse_progressive_targets("25,100,50")


@pytest.mark.parametrize("raw", ["", "0,10", "ten,20", "10,,20"])
def test_progressive_targets_reject_invalid_values(raw: str) -> None:
    with pytest.raises(LoadConfigurationError):
        parse_progressive_targets(raw)


def test_sse_stream_requires_json_chunks_and_done_marker() -> None:
    summary = validate_sse_stream(
        [
            bytearray(b'data: {"choices":[{"delta":{"role":"assistant"}}]}'),
            "",
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            "data: [DONE]",
        ]
    )

    assert summary.chunk_count == 2
    assert summary.content_chunk_count == 1


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (['data: {"choices":[]}'], "missing the \\[DONE\\] marker"),
        (["data: not-json", "data: [DONE]"], "invalid JSON"),
        (['data: {"error":{"message":"provider failed"}}', "data: [DONE]"], "error event"),
        (["data: [DONE]"], "no JSON chunks"),
        (
            ['data: {"choices":[{"delta":{"role":"assistant"}}]}', "data: [DONE]"],
            "no output content",
        ),
        (
            ['data: {"choices":[{"delta":{"content":""}}]}', "data: [DONE]"],
            "no output content",
        ),
    ],
)
def test_sse_stream_rejects_incomplete_or_invalid_responses(
    lines: list[str],
    message: str,
) -> None:
    with pytest.raises(LoadResponseError, match=message):
        validate_sse_stream(lines)


def test_sse_stream_rejects_an_unbounded_line() -> None:
    with pytest.raises(LoadResponseError, match="byte limit"):
        list(
            load_test_settings.iter_bounded_lines(
                ["data: " + ("x" * 101)],
                max_bytes=100,
            )
        )


def test_progressive_runner_builds_locked_secret_free_commands(tmp_path: Path) -> None:
    command = build_locust_command(
        output_directory=tmp_path,
        mode="chat-stream",
        target_rps=300,
    )

    assert command[:7] == [
        "uv",
        "run",
        "--locked",
        "--no-sync",
        "--group",
        "load",
        "locust",
    ]
    assert FAKE_API_KEY not in " ".join(command)
    assert str(tmp_path / "chat-stream-300") in command


def test_stage_environment_is_new_and_selects_mode_and_target() -> None:
    original = {
        "LOAD_API_KEY": FAKE_API_KEY,
        "LOAD_MODEL": "configured-model",
        "LOAD_CHAT_MAX_P95_MS": "500",
        "LOAD_STREAM_MAX_P95_MS": "750",
        "LOAD_STREAM_MAX_TTFT_MS": "250",
    }

    stage = build_stage_environment(original, mode="chat-stream", target_rps=150)

    assert stage is not original
    assert original.get("LOAD_MODE") is None
    assert stage["LOAD_MODE"] == "chat-stream"
    assert stage["LOAD_TARGET_RPS"] == "150"
    assert stage["LOAD_API_KEY"] == FAKE_API_KEY
    assert stage["LOAD_MAX_P95_MS"] == "750"
    assert stage["LOAD_MAX_TTFT_MS"] == "250"

    chat_stage = build_stage_environment(original, mode="chat", target_rps=100)
    assert chat_stage["LOAD_MAX_P95_MS"] == "500"
    assert "LOAD_MAX_TTFT_MS" not in chat_stage


def test_profile_modes_are_explicit_ordered_and_unique() -> None:
    assert parse_profile_modes("chat-stream") == ("chat-stream",)
    assert parse_profile_modes("chat, chat-stream") == ("chat", "chat-stream")

    for invalid in ("", "readiness", "chat,chat", "chat,"):
        with pytest.raises(LoadConfigurationError):
            parse_profile_modes(invalid)


def test_profile_policy_is_validated() -> None:
    assert parse_profile_policy("fail-fast") == "fail-fast"
    assert parse_profile_policy("diagnostic") == "diagnostic"

    with pytest.raises(LoadConfigurationError):
        parse_profile_policy("continue-maybe")


def test_diagnostic_policy_runs_every_stage_but_preserves_failure() -> None:
    seen: list[tuple[str, float]] = []

    def run_stage(mode: str, target: float) -> int:
        seen.append((mode, target))
        return 1 if target == 100 else 0

    outcomes = execute_stages(
        modes=("chat", "chat-stream"),
        targets=(100.0, 200.0),
        policy="diagnostic",
        run_stage=run_stage,
    )

    assert seen == [
        ("chat", 100.0),
        ("chat", 200.0),
        ("chat-stream", 100.0),
        ("chat-stream", 200.0),
    ]
    assert [outcome.returncode for outcome in outcomes] == [1, 0, 1, 0]


def test_fail_fast_policy_stops_after_first_failed_stage() -> None:
    seen: list[tuple[str, float]] = []

    def run_stage(mode: str, target: float) -> int:
        seen.append((mode, target))
        return 1

    outcomes = execute_stages(
        modes=("chat", "chat-stream"),
        targets=(100.0, 200.0),
        policy="fail-fast",
        run_stage=run_stage,
    )

    assert seen == [("chat", 100.0)]
    assert len(outcomes) == 1
    assert outcomes[0].returncode == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://gateway.example.com", "https://gateway.example.com"),
    ],
)
def test_load_host_accepts_loopback_http_or_remote_https(raw: str, expected: str) -> None:
    assert validate_load_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://gateway.example.com",
        "https://user:password@gateway.example.com",  # pragma: allowlist secret
        "https://gateway.example.com/path",
        "ftp://gateway.example.com",
    ],
)
def test_load_host_rejects_secret_leak_prone_targets(raw: str) -> None:
    with pytest.raises(LoadConfigurationError):
        validate_load_host(raw)


def test_provider_budget_is_a_conservative_upper_bound() -> None:
    budget = estimate_provider_budget(
        targets=(25.0, 50.0, 100.0),
        modes=("chat", "chat-stream"),
        duration_seconds=60,
        ramp_seconds=10,
        settle_seconds=5,
        max_tokens=8,
        prompt="OK",
        max_attempts=3,
        chat_expected_latency_seconds=1,
        stream_expected_latency_seconds=3,
        user_headroom=1.25,
    )

    steady_bound = math.ceil((25 + 50 + 100) * 75 * 2)
    initial_user_burst = sum(
        math.ceil(target * latency * 1.25) for target in (25, 50, 100) for latency in (1, 3)
    )
    assert budget.gateway_request_count == steady_bound + initial_user_burst
    assert budget.provider_attempt_count == budget.gateway_request_count * 3
    assert budget.max_input_tokens >= budget.provider_attempt_count * len("OK")
    assert budget.max_output_tokens == budget.provider_attempt_count * 8
    assert budget.max_total_tokens == budget.max_input_tokens + budget.max_output_tokens


def test_provider_budget_honors_a_single_selected_mode() -> None:
    both = estimate_provider_budget(
        targets=(100.0,),
        modes=("chat", "chat-stream"),
        duration_seconds=10,
        ramp_seconds=0,
        settle_seconds=0,
        max_tokens=8,
        prompt="OK",
        max_attempts=1,
        chat_expected_latency_seconds=1,
        stream_expected_latency_seconds=3,
        user_headroom=1.25,
    )
    chat_only = estimate_provider_budget(
        targets=(100.0,),
        modes=("chat",),
        duration_seconds=10,
        ramp_seconds=0,
        settle_seconds=0,
        max_tokens=8,
        prompt="OK",
        max_attempts=1,
        chat_expected_latency_seconds=1,
        stream_expected_latency_seconds=3,
        user_headroom=1.25,
    )

    assert chat_only.gateway_request_count < both.gateway_request_count
