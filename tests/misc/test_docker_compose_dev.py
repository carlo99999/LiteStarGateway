"""Contract tests for the local Docker Compose development stack."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.dev.yml"
DEV_DOCKERFILE = ROOT / "Dockerfile.dev"


def _compose_config() -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")

    version = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        check=False,
        text=True,
    )
    if version.returncode != 0:
        pytest.skip("Docker Compose plugin is not installed")

    environment = {
        **os.environ,
        "POSTGRES_PASSWORD": "compose-test-password",
        "MASTER_KEY": "compose-test-master-key",
        "JWT_SECRET": "compose-test-jwt-secret",
        "SALT_KEY": "compose-test-salt-key",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _volume_for(service: dict[str, Any], target: str) -> dict[str, Any]:
    return next(volume for volume in service["volumes"] if volume["target"] == target)


def test_dev_compose_provides_live_backend_frontend_and_dependencies() -> None:
    config = _compose_config()
    services = config["services"]

    assert {"db", "redis", "mlflow", "backend", "frontend"} <= services.keys()

    db = services["db"]
    assert db["image"].startswith("postgres:17@sha256:")
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert _volume_for(db, "/var/lib/postgresql/data")["type"] == "volume"

    redis = services["redis"]
    assert redis["image"].startswith("redis:7-alpine@sha256:")

    mlflow = services["mlflow"]
    assert mlflow["build"]["dockerfile"] == DEV_DOCKERFILE.name
    assert mlflow["build"]["target"] == "mlflow"
    assert "--allowed-hosts mlflow:5000,localhost,127.0.0.1" in " ".join(mlflow["command"])
    assert "/health" in " ".join(mlflow["healthcheck"]["test"])
    assert "ports" not in mlflow

    backend = services["backend"]
    assert backend["build"]["dockerfile"] == DEV_DOCKERFILE.name
    assert backend["build"]["target"] == "backend"
    assert backend["environment"]["ENVIRONMENT"] == "development"
    assert "@db:5432/gateway" in backend["environment"]["DATABASE_URL"]
    backend_command = " ".join(backend["command"])
    assert "database upgrade" in backend_command
    assert "--reload" in backend_command
    assert "--host 0.0.0.0" in backend_command
    assert "/health/ready" in " ".join(backend["healthcheck"]["test"])
    assert backend["depends_on"]["db"]["condition"] == "service_healthy"
    assert backend["depends_on"]["mlflow"]["condition"] == "service_healthy"
    assert backend["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "8000",
            "target": 8000,
        }
    ]
    assert _volume_for(backend, "/app/src")["type"] == "bind"
    assert _volume_for(backend, "/app/src")["read_only"] is True
    assert _volume_for(backend, "/app/migrations")["type"] == "bind"
    assert _volume_for(backend, "/app/migrations")["read_only"] is True
    assert not any(
        volume["type"] == "bind" and volume["target"] == "/app" for volume in backend["volumes"]
    )

    frontend = services["frontend"]
    assert frontend["build"]["dockerfile"] == DEV_DOCKERFILE.name
    assert frontend["build"]["target"] == "frontend"
    assert frontend["environment"]["GATEWAY_URL"] == "http://backend:8000"
    frontend_command = " ".join(frontend["command"])
    assert "pnpm install --frozen-lockfile" in frontend_command
    assert "pnpm dev --host 0.0.0.0" in frontend_command
    assert frontend["depends_on"]["backend"]["condition"] == "service_healthy"
    assert frontend["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "5173",
            "target": 5173,
        }
    ]
    assert _volume_for(frontend, "/app/ui")["type"] == "bind"
    assert _volume_for(frontend, "/app/ui")["read_only"] is True
    assert _volume_for(frontend, "/app/ui/node_modules")["type"] == "volume"
    assert "/ui/" in " ".join(frontend["healthcheck"]["test"])


def test_dev_compose_requires_secrets_instead_of_storing_them() -> None:
    compose_source = COMPOSE_FILE.read_text(encoding="utf-8")

    for variable in ("POSTGRES_PASSWORD", "MASTER_KEY", "JWT_SECRET", "SALT_KEY"):
        assert f"${{{variable}:?" in compose_source

    dockerfile_source = DEV_DOCKERFILE.read_text(encoding="utf-8")
    assert "USER app" in dockerfile_source
    assert "USER node" in dockerfile_source
    assert "USER mlflow" in dockerfile_source

    dockerignore_source = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env*" in dockerignore_source
    assert "!.env.sample" in dockerignore_source


def test_dev_script_drops_shell_secret_overrides(tmp_path: Path) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text(
        "\n".join(
            (
                f"POSTGRES_PASSWORD={'a' * 32}",
                f"MASTER_KEY={'b' * 32}",
                f"JWT_SECRET={'c' * 32}",
                f"SALT_KEY={'d' * 32}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "info" ]; then
  exit 0
fi
printf '%s\\n' "${POSTGRES_PASSWORD-unset}" "${MASTER_KEY-unset}" \
  "${JWT_SECRET-unset}" "${SALT_KEY-unset}"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)

    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "DEV_ENV_FILE": str(env_file),
        "POSTGRES_PASSWORD": "bad@override",
        "MASTER_KEY": "short",
        "JWT_SECRET": "short",
        "SALT_KEY": "short",
    }
    result = subprocess.run(
        [str(ROOT / "scripts" / "dev-compose.sh"), "config"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["unset", "unset", "unset", "unset"]


def test_dev_script_rejects_symlinked_secret_file(tmp_path: Path) -> None:
    real_env = tmp_path / "real.env"
    real_env.write_text("not-used=true\n", encoding="utf-8")
    linked_env = tmp_path / "linked.env"
    linked_env.symlink_to(real_env)

    result = subprocess.run(
        [str(ROOT / "scripts" / "dev-compose.sh"), "config"],
        cwd=ROOT,
        env={**os.environ, "DEV_ENV_FILE": str(linked_env)},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "symbolic link" in result.stderr


def test_dev_script_rejects_interpolated_or_duplicate_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / "unsafe.env"
    env_file.write_text(
        "\n".join(
            (
                f"POSTGRES_PASSWORD={'a' * 32}",
                f"MASTER_KEY={'b' * 32}",
                f"JWT_SECRET={'c' * 32}${{SHORT_SECRET}}",
                f"SALT_KEY={'d' * 32}",
                f"SALT_KEY={'e' * 32}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "scripts" / "dev-compose.sh"), "config"],
        cwd=ROOT,
        env={**os.environ, "DEV_ENV_FILE": str(env_file)},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "exactly once" in result.stderr or "URL-safe" in result.stderr
