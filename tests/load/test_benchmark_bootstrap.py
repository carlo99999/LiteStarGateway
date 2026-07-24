from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
benchmark = importlib.import_module("run_benchmark_contract")
bootstrap_benchmark = benchmark.bootstrap_benchmark


def test_bootstrap_builds_an_isolated_mock_model_without_exposing_the_key() -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.method, request.url.path, payload))
        responses = {
            "/login": {"access_token": "admin-jwt"},
            "/credentials": {"id": "credential-id"},
            "/organizations": {"id": "organization-id"},
            "/organizations/organization-id/teams": {"id": "team-id"},
            "/teams/team-id/keys": {"plaintext": "generated-team-key"},
            "/teams/team-id/models": {"id": "model-id"},
        }
        return httpx.Response(201, json=responses[request.url.path])

    with httpx.Client(
        base_url="http://gateway.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        identity = bootstrap_benchmark(
            client,
            admin_email="benchmark-admin@example.invalid",
            master_key="benchmark-master-key",
        )

    assert identity.model == "benchmark-mock"
    assert identity.api_key == "generated-team-key"  # pragma: allowlist secret
    assert "generated-team-key" not in repr(identity)
    assert seen[1] == (
        "POST",
        "/credentials",
        {
            "name": "benchmark-mock",
            "provider": "openai",
            "values": {
                "api_key": "mock-not-a-provider-secret",  # pragma: allowlist secret
                "api_base": "http://mock:9000/v1",
            },
        },
    )
    assert seen[-1][2] == {
        "name": "benchmark-mock",
        "provider": "openai",
        "credential_id": "credential-id",
        "type": "chat",
        "provider_model_id": "benchmark-mock",
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
    }


def test_contract_main_passes_ephemeral_key_only_in_child_environment(monkeypatch) -> None:
    marker = "ephemeral-generated-team-key"  # pragma: allowlist secret
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client"] = kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setenv("ADMIN_EMAIL", "benchmark-admin@example.invalid")
    monkeypatch.setenv("MASTER_KEY", "benchmark-master-key")
    monkeypatch.setattr(benchmark.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        benchmark,
        "bootstrap_benchmark",
        lambda *args, **kwargs: benchmark.BenchmarkIdentity("benchmark-mock", marker),
    )
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    assert benchmark.main() == 0
    command = captured["command"]
    environment = captured["environment"]
    assert isinstance(command, list)
    assert isinstance(environment, dict)
    assert marker not in " ".join(command)
    assert environment["LOAD_API_KEY"] == marker
    assert "MASTER_KEY" not in environment
    assert "ADMIN_EMAIL" not in environment


def test_contract_main_rejects_missing_bootstrap_secrets(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("MASTER_KEY", raising=False)

    assert benchmark.main() == 2
    assert "required" in capsys.readouterr().err
