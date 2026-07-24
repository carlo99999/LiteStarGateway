from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
load_artifacts = importlib.import_module("load_artifacts")
DockerStatsSampler = load_artifacts.DockerStatsSampler
build_safe_run_metadata = load_artifacts.build_safe_run_metadata
git_metadata = load_artifacts.git_metadata
inspect_containers = load_artifacts.inspect_containers
parse_docker_stats = load_artifacts.parse_docker_stats


def test_run_metadata_whitelists_configuration_and_never_serializes_secrets() -> None:
    marker = "do-not-persist-this-load-key"  # pragma: allowlist secret
    metadata = build_safe_run_metadata(
        {
            "LOAD_API_KEY": marker,
            "MASTER_KEY": "also-secret",  # pragma: allowlist secret
            "LOAD_MODEL": "benchmark-mock",
            "LOAD_MODES": "chat,chat-stream",
            "LOAD_STAGES": "100,200,300",
            "LOAD_PROFILE_POLICY": "diagnostic",
            "UVICORN_WORKERS": "3",
        },
        commit="abc123",
        dirty=False,
        containers={"gateway": {"image_id": "sha256:image", "cpus": 3, "memory_bytes": 4096}},
        report_directory="load-results/run",
        commands=(("locust", "--host", "http://127.0.0.1:8000"),),
    )

    encoded = json.dumps(metadata)
    assert metadata["git"] == {"commit": "abc123", "dirty": False}
    assert metadata["load"]["LOAD_MODEL"] == "benchmark-mock"
    assert metadata["load"]["LOAD_MODES"] == "chat,chat-stream"
    assert metadata["reports"] == {
        "directory": "load-results/run",
        "commands": [["locust", "--host", "http://127.0.0.1:8000"]],
    }
    assert marker not in encoded
    assert "also-secret" not in encoded
    assert "LOAD_API_KEY" not in encoded
    assert "MASTER_KEY" not in encoded


def test_docker_stats_parser_normalizes_cpu_and_memory_units() -> None:
    sample = parse_docker_stats(
        {
            "Name": "benchmark-app-1",
            "CPUPerc": "123.45%",
            "MemUsage": "1.5GiB / 4GiB",
        },
        role="gateway",
        observed_at="2026-07-24T12:00:00Z",
    )

    assert sample == {
        "observed_at": "2026-07-24T12:00:00Z",
        "role": "gateway",
        "container": "benchmark-app-1",
        "cpu_percent": 123.45,
        "memory_bytes": 1610612736,
        "memory_limit_bytes": 4294967296,
    }


def test_git_and_container_metadata_tolerate_missing_docker_objects(monkeypatch) -> None:
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="abc123\n"),
            SimpleNamespace(returncode=0, stdout=" M file\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Name": "/benchmark-app-1",
                        "Image": "sha256:image",
                        "HostConfig": {"NanoCpus": 3_000_000_000, "Memory": 4096},
                    }
                ),
            ),
            SimpleNamespace(returncode=1, stdout=""),
        ]
    )
    monkeypatch.setattr(load_artifacts.subprocess, "run", lambda *args, **kwargs: next(responses))

    assert git_metadata() == ("abc123", True)
    assert inspect_containers({"gateway": "app-id", "missing": "missing-id"}) == {
        "gateway": {
            "container": "benchmark-app-1",
            "image_id": "sha256:image",
            "nano_cpus": 3_000_000_000,
            "memory_bytes": 4096,
        }
    }


def test_resource_sampler_writes_normalized_json_lines(tmp_path, monkeypatch) -> None:
    response = SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "Name": "benchmark-app-1",
                "CPUPerc": "12.5%",
                "MemUsage": "10MiB / 4GiB",
            }
        ),
    )
    monkeypatch.setattr(load_artifacts.subprocess, "run", lambda *args, **kwargs: response)
    destination = tmp_path / "resources.jsonl"
    sampler = DockerStatsSampler(
        containers={"gateway": "app-id"},
        destination=destination,
        interval_seconds=0.001,
    )

    sampler.start()
    for _ in range(100):
        if destination.exists() and destination.stat().st_size:
            break
        load_artifacts.threading.Event().wait(0.001)
    sampler.stop()

    samples = [json.loads(line) for line in destination.read_text().splitlines()]
    assert samples
    assert samples[0]["role"] == "gateway"
    assert samples[0]["cpu_percent"] == 12.5
