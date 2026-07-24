"""Bootstrap the isolated benchmark stack and run its deterministic load contract."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

BOOTSTRAP_ONLY_ENVIRONMENT = frozenset(
    {"POSTGRES_PASSWORD", "MASTER_KEY", "JWT_SECRET", "SALT_KEY", "ADMIN_EMAIL"}
)


@dataclass(frozen=True)
class BenchmarkIdentity:
    """Ephemeral team identity returned once by the isolated management plane."""

    model: str
    api_key: str = field(repr=False)


def _post(
    client: httpx.Client,
    path: str,
    payload: Mapping[str, object],
    *,
    token: str | None = None,
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    response = client.post(path, json=dict(payload), headers=headers)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"benchmark bootstrap received a non-object response from {path}")
    return value


def _required_string(body: Mapping[str, object], key: str, *, endpoint: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"benchmark bootstrap response from {endpoint} omitted {key}")
    return value


def bootstrap_benchmark(
    client: httpx.Client,
    *,
    admin_email: str,
    master_key: str,
) -> BenchmarkIdentity:
    """Create one credential, organization, team, API key and model through public APIs."""

    login = _post(
        client,
        "/login",
        {"email": admin_email, "password": master_key},
    )
    token = _required_string(login, "access_token", endpoint="/login")
    credential = _post(
        client,
        "/credentials",
        {
            "name": "benchmark-mock",
            "provider": "openai",
            "values": {
                "api_key": "mock-not-a-provider-secret",  # pragma: allowlist secret
                "api_base": "http://mock:9000/v1",
            },
        },
        token=token,
    )
    credential_id = _required_string(credential, "id", endpoint="/credentials")
    organization = _post(
        client,
        "/organizations",
        {"name": "Benchmark"},
        token=token,
    )
    organization_id = _required_string(organization, "id", endpoint="/organizations")
    team_endpoint = f"/organizations/{organization_id}/teams"
    team = _post(
        client,
        team_endpoint,
        {
            "name": "Benchmark",
            "admin_email": admin_email,
            "rate_limit_rpm": None,
        },
        token=token,
    )
    team_id = _required_string(team, "id", endpoint=team_endpoint)
    key_endpoint = f"/teams/{team_id}/keys"
    created_key = _post(
        client,
        key_endpoint,
        {"name": "benchmark", "scope": "inference", "rate_limit_rpm": None},
        token=token,
    )
    api_key = _required_string(created_key, "plaintext", endpoint=key_endpoint)
    model_endpoint = f"/teams/{team_id}/models"
    _post(
        client,
        model_endpoint,
        {
            "name": "benchmark-mock",
            "provider": "openai",
            "credential_id": credential_id,
            "type": "chat",
            "provider_model_id": "benchmark-mock",
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        token=token,
    )
    return BenchmarkIdentity(model="benchmark-mock", api_key=api_key)


def main() -> int:
    admin_email = os.environ.get("ADMIN_EMAIL", "")
    master_key = os.environ.get("MASTER_KEY", "")
    host = os.environ.get("LOAD_HOST", "http://127.0.0.1:18000").rstrip("/")
    if not admin_email or not master_key:
        print(
            "benchmark configuration error: ADMIN_EMAIL and MASTER_KEY are required",
            file=sys.stderr,
        )
        return 2

    try:
        with httpx.Client(base_url=host, timeout=30) as client:
            identity = bootstrap_benchmark(
                client,
                admin_email=admin_email,
                master_key=master_key,
            )
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"benchmark bootstrap failed: {exc}", file=sys.stderr)
        return 2

    environment = {
        **{
            key: value for key, value in os.environ.items() if key not in BOOTSTRAP_ONLY_ENVIRONMENT
        },
        "LOAD_API_KEY": identity.api_key,
        "LOAD_MODEL": identity.model,
        "LOAD_CONFIRM_PROVIDER_COST": "YES",
        "LOAD_PROVIDER_MAX_ATTEMPTS": "1",
    }
    print("Benchmark fixture created; starting deterministic profile.", flush=True)
    return subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "--no-sync",
            "--group",
            "load",
            "python",
            "scripts/run_load_profile.py",
        ],
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
