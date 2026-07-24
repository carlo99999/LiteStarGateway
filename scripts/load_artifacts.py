"""Secret-safe metadata and Docker resource samples for load-test runs."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SAFE_LOAD_KEYS = (
    "LOAD_MODEL",
    "LOAD_MODES",
    "LOAD_STAGES",
    "LOAD_PROFILE_POLICY",
    "LOAD_DURATION_SECONDS",
    "LOAD_RAMP_SECONDS",
    "LOAD_SETTLE_SECONDS",
    "LOAD_CHAT_EXPECTED_LATENCY_SECONDS",
    "LOAD_STREAM_EXPECTED_LATENCY_SECONDS",
    "LOAD_USER_HEADROOM",
    "LOAD_MAX_TOKENS",
    "LOAD_PROVIDER_MAX_ATTEMPTS",
    "LOAD_MAX_FAILURE_RATIO",
    "LOAD_MIN_RPS_RATIO",
    "LOAD_CHAT_MAX_P95_MS",
    "LOAD_STREAM_MAX_P95_MS",
    "LOAD_STREAM_MAX_TTFT_MS",
    "LOAD_MOCK_TTFT_MS",
    "LOAD_MOCK_CHUNK_INTERVAL_MS",
    "LOAD_MOCK_TOTAL_LATENCY_MS",
    "LOAD_MOCK_CHUNK_COUNT",
    "LOAD_MOCK_FAILURE_EVERY",
    "LOAD_MOCK_FAILURE_STATUS",
    "UVICORN_WORKERS",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
)
_BYTE_FACTORS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def build_safe_run_metadata(
    environment: Mapping[str, str],
    *,
    commit: str,
    dirty: bool,
    containers: Mapping[str, Mapping[str, Any]],
    report_directory: str | None = None,
    commands: tuple[tuple[str, ...], ...] = (),
) -> dict[str, Any]:
    """Build metadata from an allowlist; never serialize process environment wholesale."""

    load = {key: environment[key] for key in SAFE_LOAD_KEYS if key in environment}
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "runner": "scripts/run_load_profile.py",
        "git": {"commit": commit, "dirty": dirty},
        "load": load,
        "containers": {role: dict(values) for role, values in containers.items()},
        "reports": {
            "directory": report_directory,
            "commands": [list(command) for command in commands],
        },
    }


def _bytes(raw: str) -> int:
    value = raw.strip().replace(" ", "")
    number = ""
    unit = ""
    for character in value:
        if character.isdigit() or character in {".", "-"}:
            number += character
        else:
            unit += character
    factor = _BYTE_FACTORS.get(unit.upper())
    if not number or factor is None:
        raise ValueError(f"unsupported memory value: {raw}")
    return round(float(number) * factor)


def parse_docker_stats(
    raw: Mapping[str, str],
    *,
    role: str,
    observed_at: str,
) -> dict[str, Any]:
    """Normalize one `docker stats --format json` record."""

    usage, limit = (part.strip() for part in raw["MemUsage"].split("/", maxsplit=1))
    return {
        "observed_at": observed_at,
        "role": role,
        "container": raw["Name"],
        "cpu_percent": float(raw["CPUPerc"].removesuffix("%")),
        "memory_bytes": _bytes(usage),
        "memory_limit_bytes": _bytes(limit),
    }


def git_metadata() -> tuple[str, bool]:
    """Read a short commit and dirty bit without failing a benchmark outside Git."""

    commit = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        check=False,
        text=True,
    )
    return (
        commit.stdout.strip() if commit.returncode == 0 else "unknown",
        status.returncode != 0 or bool(status.stdout.strip()),
    )


def inspect_containers(containers: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Collect only image and configured resource limits for named containers."""

    result: dict[str, dict[str, Any]] = {}
    for role, container in containers.items():
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .}}",
                container,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        if inspected.returncode:
            continue
        value = json.loads(inspected.stdout)
        host_config = value.get("HostConfig", {})
        result[role] = {
            "container": value.get("Name", "").lstrip("/"),
            "image_id": value.get("Image", "unknown"),
            "nano_cpus": host_config.get("NanoCpus", 0),
            "memory_bytes": host_config.get("Memory", 0),
        }
    return result


class DockerStatsSampler:
    """Sample Docker CPU/RSS in a background thread for the duration of a run."""

    def __init__(
        self,
        *,
        containers: Mapping[str, str],
        destination: Path,
        interval_seconds: float = 1.0,
    ) -> None:
        self._containers = dict(containers)
        self._destination = destination
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._containers:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5, self._interval_seconds * 2))

    def _run(self) -> None:
        self._destination.parent.mkdir(parents=True, exist_ok=True)
        with self._destination.open("a", encoding="utf-8") as output:
            while not self._stop.is_set():
                observed_at = datetime.now(UTC).isoformat()
                for role, container in self._containers.items():
                    completed = subprocess.run(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            container,
                        ],
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    if completed.returncode:
                        continue
                    try:
                        sample = parse_docker_stats(
                            json.loads(completed.stdout),
                            role=role,
                            observed_at=observed_at,
                        )
                    except KeyError, TypeError, ValueError, json.JSONDecodeError:
                        continue
                    output.write(json.dumps(sample, separators=(",", ":")) + "\n")
                    output.flush()
                self._stop.wait(self._interval_seconds)
