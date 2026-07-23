"""Run isolated Locust stages for non-streaming and streaming chat."""

from __future__ import annotations

import ipaddress
import math
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from load_test_settings import LoadConfigurationError, parse_progressive_targets

ChatMode = Literal["chat", "chat-stream"]
PROFILE_MODES: tuple[ChatMode, ...] = ("chat", "chat-stream")


@dataclass(frozen=True)
class ProviderBudget:
    """Conservative upper bounds for one two-mode progressive profile."""

    gateway_request_count: int
    provider_attempt_count: int
    max_input_tokens: int
    max_output_tokens: int

    @property
    def max_total_tokens(self) -> int:
        return self.max_input_tokens + self.max_output_tokens


def _positive_number(environment: Mapping[str, str], name: str, default: str) -> float:
    raw = environment.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise LoadConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise LoadConfigurationError(f"{name} must be greater than zero")
    return value


def _nonnegative_number(environment: Mapping[str, str], name: str, default: str) -> float:
    raw = environment.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise LoadConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise LoadConfigurationError(f"{name} must be non-negative")
    return value


def _positive_integer(environment: Mapping[str, str], name: str, default: str) -> int:
    raw = environment.get(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise LoadConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise LoadConfigurationError(f"{name} must be greater than zero")
    return value


def validate_load_host(raw: str) -> str:
    """Require HTTPS except for an explicit loopback benchmark target."""

    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LoadConfigurationError("LOAD_HOST must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise LoadConfigurationError("LOAD_HOST must not contain user information")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise LoadConfigurationError("LOAD_HOST must contain only scheme, host, and port")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise LoadConfigurationError("LOAD_HOST has an invalid port") from exc

    hostname = parsed.hostname
    try:
        loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise LoadConfigurationError("LOAD_HOST must use HTTPS unless it targets loopback")
    return raw.rstrip("/")


def estimate_provider_budget(
    *,
    targets: tuple[float, ...],
    duration_seconds: float,
    ramp_seconds: float,
    settle_seconds: float,
    max_tokens: int,
    prompt: str,
    max_attempts: int,
    chat_expected_latency_seconds: float,
    stream_expected_latency_seconds: float,
    user_headroom: float,
) -> ProviderBudget:
    """Bound calls as if every stage ran at full target for its entire window."""

    seconds_per_stage = duration_seconds + ramp_seconds + settle_seconds
    paced_requests = math.ceil(sum(targets) * seconds_per_stage * len(PROFILE_MODES))
    initial_user_burst = sum(
        math.ceil(target * latency * user_headroom)
        for target in targets
        for latency in (chat_expected_latency_seconds, stream_expected_latency_seconds)
    )
    gateway_requests = paced_requests + initial_user_burst
    provider_attempts = gateway_requests * max_attempts
    max_input_tokens_per_attempt = len(prompt.encode("utf-8")) + 128
    return ProviderBudget(
        gateway_request_count=gateway_requests,
        provider_attempt_count=provider_attempts,
        max_input_tokens=provider_attempts * max_input_tokens_per_attempt,
        max_output_tokens=provider_attempts * max_tokens,
    )


def _target_label(target_rps: float) -> str:
    return f"{target_rps:g}".replace(".", "p")


def build_locust_command(
    *,
    output_directory: Path,
    mode: ChatMode,
    target_rps: float,
    host: str = "http://127.0.0.1:8000",
) -> list[str]:
    """Build a lockfile-only command whose arguments never contain credentials."""

    prefix = output_directory / f"{mode}-{_target_label(target_rps)}"
    return [
        "uv",
        "run",
        "--locked",
        "--no-sync",
        "--group",
        "load",
        "locust",
        "-f",
        "scripts/locustfile.py",
        "--headless",
        "--host",
        host,
        "--csv",
        str(prefix),
        "--html",
        f"{prefix}.html",
    ]


def build_stage_environment(
    environment: Mapping[str, str],
    *,
    mode: ChatMode,
    target_rps: float,
) -> dict[str, str]:
    """Return a fresh environment configured for one mode and RPS stage."""

    stage = {
        **environment,
        "LOAD_MODE": mode,
        "LOAD_TARGET_RPS": f"{target_rps:g}",
    }
    latency_override = (
        "LOAD_STREAM_EXPECTED_LATENCY_SECONDS"
        if mode == "chat-stream"
        else "LOAD_CHAT_EXPECTED_LATENCY_SECONDS"
    )
    if latency_override in environment:
        stage["LOAD_EXPECTED_LATENCY_SECONDS"] = environment[latency_override]
    return stage


def main() -> int:
    environment = dict(os.environ)
    if environment.get("LOAD_CONFIRM_PROVIDER_COST") != "YES":
        print(
            "load-test configuration error: LOAD_CONFIRM_PROVIDER_COST must be exactly YES",
            file=sys.stderr,
        )
        return 2
    if not environment.get("LOAD_API_KEY"):
        print("load-test configuration error: LOAD_API_KEY is required", file=sys.stderr)
        return 2
    if not environment.get("LOAD_MODEL"):
        print("load-test configuration error: LOAD_MODEL is required", file=sys.stderr)
        return 2

    try:
        targets = parse_progressive_targets(
            environment.get("LOAD_STAGES", "25,50,100,150,200,250,300")
        )
        duration = _positive_number(environment, "LOAD_DURATION_SECONDS", "60")
        ramp = _nonnegative_number(environment, "LOAD_RAMP_SECONDS", "10")
        settle = _nonnegative_number(environment, "LOAD_SETTLE_SECONDS", "5")
        max_tokens = _positive_integer(environment, "LOAD_MAX_TOKENS", "8")
        max_attempts = _positive_integer(
            environment,
            "LOAD_PROVIDER_MAX_ATTEMPTS",
            "3",
        )
        default_expected_latency = environment.get("LOAD_EXPECTED_LATENCY_SECONDS")
        chat_expected_latency = _positive_number(
            environment,
            "LOAD_CHAT_EXPECTED_LATENCY_SECONDS",
            default_expected_latency or "1",
        )
        stream_expected_latency = _positive_number(
            environment,
            "LOAD_STREAM_EXPECTED_LATENCY_SECONDS",
            default_expected_latency or "3",
        )
        user_headroom = _positive_number(environment, "LOAD_USER_HEADROOM", "1.25")
        max_requests = _positive_integer(
            environment,
            "LOAD_MAX_PROVIDER_REQUESTS",
            "600000",
        )
        max_provider_tokens = _positive_integer(
            environment,
            "LOAD_MAX_PROVIDER_TOKENS",
            "100000000",
        )
        host = validate_load_host(environment.get("LOAD_HOST", "http://127.0.0.1:8000"))
    except LoadConfigurationError as exc:
        print(f"load-test configuration error: {exc}", file=sys.stderr)
        return 2

    budget = estimate_provider_budget(
        targets=targets,
        duration_seconds=duration,
        ramp_seconds=ramp,
        settle_seconds=settle,
        max_tokens=max_tokens,
        prompt=environment.get("LOAD_PROMPT", "Reply with the single word OK."),
        max_attempts=max_attempts,
        chat_expected_latency_seconds=chat_expected_latency,
        stream_expected_latency_seconds=stream_expected_latency,
        user_headroom=user_headroom,
    )
    if budget.provider_attempt_count > max_requests:
        print(
            "load-test configuration error:"
            f" provider-attempt bound {budget.provider_attempt_count} exceeds"
            f" LOAD_MAX_PROVIDER_REQUESTS={max_requests}",
            file=sys.stderr,
        )
        return 2
    if budget.max_total_tokens > max_provider_tokens:
        print(
            "load-test configuration error:"
            f" total-token bound {budget.max_total_tokens} exceeds"
            f" LOAD_MAX_PROVIDER_TOKENS={max_provider_tokens}",
            file=sys.stderr,
        )
        return 2

    run_directory = Path("load-results") / time.strftime("%Y%m%d-%H%M%S")
    run_directory.mkdir(parents=True, exist_ok=False)
    print(
        f"Provider safety bounds: <= {budget.gateway_request_count} gateway requests,"
        f" <= {budget.provider_attempt_count} provider attempts,"
        f" <= {budget.max_total_tokens} total tokens",
        flush=True,
    )

    for mode in PROFILE_MODES:
        for target in targets:
            print(f"\n=== {mode}: {target:g} RPS ===", flush=True)
            command = build_locust_command(
                output_directory=run_directory,
                mode=mode,
                target_rps=target,
                host=host,
            )
            stage_environment = build_stage_environment(
                environment,
                mode=mode,
                target_rps=target,
            )
            completed = subprocess.run(command, env=stage_environment, check=False)
            if completed.returncode:
                print(
                    f"Profile stopped at the first failed stage: {mode} {target:g} RPS",
                    file=sys.stderr,
                )
                return completed.returncode

    print(f"\nAll progressive stages passed. Reports: {run_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
