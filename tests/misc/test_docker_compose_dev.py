"""Contract tests for the local Docker Compose development stack."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.dev.yml"
DEV_DOCKERFILE = ROOT / "Dockerfile.dev"
PROD_COMPOSE_FILE = ROOT / "docker-compose.yml"
PROD_DOCKERFILE = ROOT / "Dockerfile"


def _compose_config(
    compose_file: Path = COMPOSE_FILE,
    extra_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
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
        "POSTGRES_PASSWORD": "compose-test-password",  # pragma: allowlist secret
        "MASTER_KEY": "compose-test-master-key",  # pragma: allowlist secret
        "JWT_SECRET": "compose-test-jwt-secret",  # pragma: allowlist secret
        "SALT_KEY": "compose-test-salt-key",  # pragma: allowlist secret
        "MLFLOW_TRACKING_URI": "",
        **(extra_environment or {}),
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
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

    assert set(services) == {"db", "redis", "backend", "frontend"}

    db = services["db"]
    assert db["image"].startswith("postgres:17@sha256:")
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert _volume_for(db, "/var/lib/postgresql/data")["type"] == "volume"

    redis = services["redis"]
    assert redis["image"].startswith("redis:7-alpine@sha256:")

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
    assert set(backend["depends_on"]) == {"db", "redis"}
    assert backend["environment"]["MLFLOW_TRACKING_URI"] == ""
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
    assert "pnpm install" not in frontend_command
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
    node_modules = _volume_for(frontend, "/app/ui/node_modules")
    assert node_modules["type"] == "volume"
    assert node_modules["source"] == "ui-dev-node-modules"
    assert config["volumes"]["ui-dev-node-modules"]["name"] == (
        "litestar-gateway-dev_ui-dev-node-modules"
    )
    fingerprinted = _compose_config(
        extra_environment={"UI_NODE_MODULES_VOLUME": "litestar-gateway-dev_ui-dev-node-modules-123"}
    )
    assert fingerprinted["volumes"]["ui-dev-node-modules"]["name"].endswith("-123")
    assert "/ui/" in " ".join(frontend["healthcheck"]["test"])


def test_prod_compose_keeps_the_ui_without_bundling_mlflow() -> None:
    config = _compose_config(PROD_COMPOSE_FILE)
    dev_config = _compose_config()
    services = config["services"]

    postgres_image = services["db"]["image"]
    redis_image = services["redis"]["image"]
    assert re.fullmatch(r"postgres:17@sha256:[0-9a-f]{64}", postgres_image)
    assert re.fullmatch(r"redis:7-alpine@sha256:[0-9a-f]{64}", redis_image)
    assert postgres_image == dev_config["services"]["db"]["image"]
    assert redis_image == dev_config["services"]["redis"]["image"]

    assert "mlflow-init" not in services
    assert "mlflow" not in services
    assert "mlflow-data" not in config["volumes"]

    app = services["app"]
    assert app["build"]["target"] == "runtime"
    assert set(app["depends_on"]) == {"db", "redis"}
    assert app["environment"]["MLFLOW_TRACKING_URI"] == ""
    assert app["deploy"]["resources"] == {
        "limits": {"cpus": 1, "memory": str(4 * 1024**3)},
        "reservations": {"cpus": 1, "memory": str(4 * 1024**3)},
    }
    external_mlflow = _compose_config(
        PROD_COMPOSE_FILE,
        {"MLFLOW_TRACKING_URI": "https://mlflow.example.com"},
    )
    assert (
        external_mlflow["services"]["app"]["environment"]["MLFLOW_TRACKING_URI"]
        == "https://mlflow.example.com"
    )
    assert "/health/ready" in " ".join(app["healthcheck"]["test"])
    assert app["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "8000",
            "target": 8000,
        }
    ]

    dockerfile_source = PROD_DOCKERFILE.read_text(encoding="utf-8")
    assert "AS mlflow" not in dockerfile_source
    assert "AS ui" in dockerfile_source
    assert "COPY --from=ui --chown=app:app /app/ui/dist /app/ui/dist" in dockerfile_source

    custom_port_config = _compose_config(PROD_COMPOSE_FILE, {"APP_PORT": "18080"})
    assert custom_port_config["services"]["app"]["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "18080",
            "target": 8000,
        }
    ]


def test_dev_compose_requires_secrets_instead_of_storing_them() -> None:
    compose_source = COMPOSE_FILE.read_text(encoding="utf-8")

    for variable in ("POSTGRES_PASSWORD", "MASTER_KEY", "JWT_SECRET", "SALT_KEY"):
        assert f"${{{variable}:?" in compose_source

    dockerfile_source = DEV_DOCKERFILE.read_text(encoding="utf-8")
    assert "USER app" in dockerfile_source
    assert "USER node" in dockerfile_source
    assert "AS mlflow" not in dockerfile_source

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
  printf '%s\\n' 'linux-arm64'
  exit 0
fi
printf '%s\\n' "${POSTGRES_PASSWORD-unset}" "${MASTER_KEY-unset}" \
  "${JWT_SECRET-unset}" "${SALT_KEY-unset}" "${UI_NODE_MODULES_VOLUME-unset}"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)

    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "DEV_ENV_FILE": str(env_file),
        "POSTGRES_PASSWORD": "bad@override",  # pragma: allowlist secret
        "MASTER_KEY": "short",  # pragma: allowlist secret
        "JWT_SECRET": "short",  # pragma: allowlist secret
        "SALT_KEY": "short",  # pragma: allowlist secret
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
    output = result.stdout.splitlines()
    assert output[:4] == ["unset", "unset", "unset", "unset"]
    assert re.fullmatch(
        r"litestar-gateway-dev_ui-dev-node-modules-\d+",
        output[4],
    )

    forced_platform_result = subprocess.run(
        [str(ROOT / "scripts" / "dev-compose.sh"), "config"],
        cwd=ROOT,
        env={**environment, "DOCKER_DEFAULT_PLATFORM": "linux/amd64"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert forced_platform_result.returncode == 0, forced_platform_result.stderr
    assert forced_platform_result.stdout.splitlines()[4] != output[4]


def test_ui_dependency_fingerprint_includes_toolchain_and_platform() -> None:
    launcher = (ROOT / "scripts" / "dev-compose.sh").read_text(encoding="utf-8")

    assert '"$ROOT/Dockerfile.dev"' in launcher
    assert "docker info --format" in launcher


def test_load_script_generates_the_ignored_prod_overlay() -> None:
    launcher = (ROOT / "scripts" / "load-compose.sh").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")

    assert "docker-compose.load.yml" in gitignore
    assert "litestar-gateway-dev_pg-dev-data" in launcher
    assert "INFERENCE_RATE_LIMIT_RPM" in launcher
    assert "MAX_RETRIES" in launcher
    assert '"$ROOT/scripts/dev-compose.sh" down' in launcher
    assert "load-prod-up:" in justfile
    assert "./scripts/load-compose.sh up" in justfile


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
