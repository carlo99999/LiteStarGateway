from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.benchmark.yml"
SCRIPT = ROOT / "scripts" / "benchmark-compose.sh"


def _config(*extra_args: str) -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *extra_args, "config", "--format", "json"],
        cwd=ROOT,
        env={
            **os.environ,
            "POSTGRES_PASSWORD": "benchmark-postgres-password",  # pragma: allowlist secret
            "MASTER_KEY": "benchmark-master-key",  # pragma: allowlist secret
            "JWT_SECRET": "benchmark-jwt-secret",  # pragma: allowlist secret
            "SALT_KEY": "benchmark-salt-key",  # pragma: allowlist secret
            "ADMIN_EMAIL": "benchmark-admin@example.invalid",
        },
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_benchmark_stack_is_isolated_bounded_and_has_no_mock_host_port() -> None:
    services = _config()["services"]

    assert set(services) == {"db", "redis", "mock", "app"}
    assert services["db"]["tmpfs"] == ["/var/lib/postgresql/data"]
    assert "ports" not in services["mock"]
    assert services["mock"]["entrypoint"] == ["python", "scripts/load_mock_server.py"]
    assert services["app"]["depends_on"]["mock"]["condition"] == "service_healthy"
    assert services["app"]["environment"]["UVICORN_WORKERS"] == "3"
    assert services["app"]["environment"]["MLFLOW_TRACKING_URI"] == ""
    assert services["app"]["environment"]["MAX_RETRIES"] == "0"
    assert services["app"]["deploy"]["resources"]["limits"] == {
        "cpus": 3,
        "memory": str(4 * 1024**3),
    }


def test_loadgen_service_is_profile_gated_with_no_ports_and_mounts_out() -> None:
    services = _config()["services"]
    assert "loadgen" not in services, "loadgen must not start via `up` without --profile loadgen"

    gated_services = _config("--profile", "loadgen")["services"]
    loadgen = gated_services["loadgen"]

    assert loadgen["profiles"] == ["loadgen"]
    assert loadgen["build"]["target"] == "loadgen"
    assert "ports" not in loadgen
    assert loadgen["volumes"] == [
        {
            "type": "bind",
            "source": str(ROOT / "load-results"),
            "target": "/out",
            "bind": {},
        }
    ]


def test_benchmark_launcher_generates_private_secrets_and_destroys_ephemeral_data() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "umask 077" in source
    assert ".env.benchmark" in source
    assert "openssl rand" in source
    assert "down --volumes" in source
    assert "docker-compose.dev.yml" not in source
    assert 'source "$ENV_FILE"' not in source
    assert "Unexpected key" in source
    assert "LOAD_CHAT_EXPECTED_LATENCY_SECONDS:-1}" in source
    assert "LOAD_STREAM_EXPECTED_LATENCY_SECONDS:-3}" in source


def test_benchmark_launcher_defaults_to_in_network_load_generation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'LOAD_IN_NETWORK="${LOAD_IN_NETWORK:-1}"' in source
    # LOAD_HOST must stay pinned to the host-published port unconditionally:
    # scripts/run_benchmark_contract.py's admin bootstrap runs on the host and
    # cannot resolve the compose-internal "app" hostname.
    assert 'export LOAD_HOST="http://127.0.0.1:${BENCHMARK_PORT:-18000}"' in source
    assert 'LOAD_STAGE_HOST="http://app:8000"' in source
    assert "compose --profile loadgen build loadgen" in source
