"""AWS Bedrock provider: Converse chat (+streaming), emulated Responses,
Titan/Cohere embeddings, Titan images — boto3 monkeypatched, no real AWS."""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EventStreamError,
    ReadTimeoutError,
)
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.config import DEFAULT_MAX_RETRIES, DEFAULT_REQUEST_TIMEOUT
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import (
    UnsupportedOperation,
    UpstreamAuthFailed,
    UpstreamRateLimited,
    UpstreamRequestRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from litestar_gateway.infrastructure.llm import bedrock_adapter
from litestar_gateway.infrastructure.llm.bedrock_adapter import (
    converse_event_to_delta,
    from_converse_response,
    to_converse_request,
)
from litestar_gateway.infrastructure.llm.errors import translate_stream, translate_upstream_error

from .conftest import _bearer, _patch, _setup, _setup_team, _team_usage

BEDROCK_VALUES = {
    "aws_access_key_id": "AKIAEXAMPLE",
    "aws_secret_access_key": "shhh",  # pragma: allowlist secret
    "region": "eu-west-1",
}


def _model(provider_model_id: str = "anthropic.claude-3-5-sonnet-v2:0") -> Model:
    from datetime import UTC, datetime
    from uuid import uuid4

    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=Provider.BEDROCK,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id=provider_model_id,
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


# ── Pure translators: chat ───────────────────────────────────────────────────


def test_to_converse_request_maps_messages_and_params() -> None:
    kwargs = to_converse_request(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
            "max_tokens": 64,
            "temperature": 0.3,
            "top_p": 0.9,
            "stop": "END",
        },
        _model(),
    )
    assert kwargs["modelId"] == "anthropic.claude-3-5-sonnet-v2:0"
    assert kwargs["system"] == [{"text": "be brief"}]
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "hello"}]},
    ]
    assert kwargs["inferenceConfig"] == {
        "maxTokens": 64,
        "temperature": 0.3,
        "topP": 0.9,
        "stopSequences": ["END"],
    }


def test_to_converse_request_omits_absent_params() -> None:
    kwargs = to_converse_request({"messages": [{"role": "user", "content": "hi"}]}, _model())
    assert "inferenceConfig" not in kwargs
    assert "system" not in kwargs
    assert "toolConfig" not in kwargs


def test_to_converse_request_json_schema_forces_a_tool() -> None:
    kwargs = to_converse_request(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            },
        },
        _model(),
    )
    tool = kwargs["toolConfig"]["tools"][0]["toolSpec"]
    assert tool["name"] == "answer"
    assert tool["inputSchema"] == {"json": {"type": "object"}}
    assert kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": "answer"}}


def test_to_converse_request_json_object_nudges_via_system() -> None:
    kwargs = to_converse_request(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
        _model(),
    )
    assert "toolConfig" not in kwargs
    assert any("JSON" in part["text"] for part in kwargs["system"])


def test_from_converse_response_maps_text_usage_and_stop_reason() -> None:
    body = from_converse_response(
        {
            "output": {
                "message": {"role": "assistant", "content": [{"text": "hi "}, {"text": "there"}]}
            },
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        },
        "anthropic.claude-3-5-sonnet-v2:0",
    )
    assert body["object"] == "chat.completion"
    assert body["model"] == "anthropic.claude-3-5-sonnet-v2:0"
    choice = body["choices"][0]
    assert choice["message"]["content"] == "hi there"
    assert choice["finish_reason"] == "length"
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}


def test_from_converse_response_surfaces_forced_tool_as_json_content() -> None:
    body = from_converse_response(
        {
            "output": {
                "message": {"content": [{"toolUse": {"name": "answer", "input": {"answer": 42}}}]}
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1, "outputTokens": 2},
        },
        "m",
    )
    choice = body["choices"][0]
    assert json.loads(choice["message"]["content"]) == {"answer": 42}
    # Structured output looks like a normal completion to the client.
    assert choice["finish_reason"] == "stop"


def test_converse_event_to_delta_mapping() -> None:
    assert converse_event_to_delta({"messageStart": {"role": "assistant"}}) == (
        {"role": "assistant"},
        None,
    )
    assert converse_event_to_delta({"contentBlockDelta": {"delta": {"text": "hi"}}}) == (
        {"content": "hi"},
        None,
    )
    # Forced structured-output tool streams partial JSON via toolUse input.
    assert converse_event_to_delta(
        {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"a":'}}}}
    ) == ({"content": '{"a":'}, None)
    assert converse_event_to_delta({"messageStop": {"stopReason": "end_turn"}}) == ({}, "stop")
    assert converse_event_to_delta({"contentBlockStop": {}}) == (None, None)
    assert converse_event_to_delta({"metadata": {"usage": {}}}) == (None, None)


# ── Error translation (botocore) ─────────────────────────────────────────────


def _client_error(status: int, code: str) -> ClientError:
    # Untyped on purpose: botocore's stubs want the full ResponseMetadata
    # TypedDict, but real error responses carry only what the service returned.
    error_response: Any = {
        "Error": {"Code": code, "Message": "x"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(error_response, "Converse")


def test_botocore_errors_translate_to_domain_errors() -> None:
    assert isinstance(
        translate_upstream_error(_client_error(429, "ThrottlingException")), UpstreamRateLimited
    )
    # Some AWS throttles surface as 400 + ThrottlingException: still a 429.
    assert isinstance(
        translate_upstream_error(_client_error(400, "ThrottlingException")), UpstreamRateLimited
    )
    assert isinstance(
        translate_upstream_error(_client_error(500, "InternalServerException")),
        UpstreamUnavailable,
    )
    assert isinstance(
        translate_upstream_error(ReadTimeoutError(endpoint_url="https://x")), UpstreamTimeout
    )
    assert isinstance(
        translate_upstream_error(ConnectTimeoutError(endpoint_url="https://x")), UpstreamTimeout
    )


def _event_stream_error(code: str) -> EventStreamError:
    # Mid-stream failures come from botocore's event-stream parser, whose error
    # response carries only Error (no ResponseMetadata, hence no HTTP status).
    error_response: Any = {"Error": {"Code": code, "Message": "x"}}
    return EventStreamError(error_response, "ConverseStream")


def test_mid_stream_errors_translate_like_non_streaming() -> None:
    # 5xx-class stream errors map to the same domain error as the equivalent
    # non-streaming ClientError (502 to the client, retryable).
    for code, status in (
        ("InternalServerException", 500),
        ("ModelStreamErrorException", 502),
        ("ServiceUnavailableException", 503),
    ):
        mapped = translate_upstream_error(_event_stream_error(code))
        assert isinstance(mapped, UpstreamUnavailable), code
        assert type(mapped) is type(translate_upstream_error(_client_error(status, code)))
    # ValidationException is the client's 400 on both paths (not retryable).
    rejected = translate_upstream_error(_event_stream_error("ValidationException"))
    assert isinstance(rejected, UpstreamRequestRejected)
    assert type(rejected) is type(
        translate_upstream_error(_client_error(400, "ValidationException"))
    )
    # Throttling keeps being rescued by error code, exactly as before.
    assert isinstance(
        translate_upstream_error(_event_stream_error("ThrottlingException")), UpstreamRateLimited
    )


def test_mid_stream_errors_not_yet_mapped_do_not_fall_through_to_500() -> None:
    # R7-L35: ModelTimeoutException, ModelNotReadyException and
    # AccessDeniedException used to be absent from `_AWS_ERROR_CODE_STATUS`, so
    # mid-stream failures with these codes fell through to an opaque 500
    # (`_status_code` returned None) instead of a classified domain error.
    timeout_mapped = translate_upstream_error(_event_stream_error("ModelTimeoutException"))
    assert isinstance(timeout_mapped, UpstreamUnavailable)

    not_ready_mapped = translate_upstream_error(_event_stream_error("ModelNotReadyException"))
    assert isinstance(not_ready_mapped, UpstreamUnavailable)

    denied_mapped = translate_upstream_error(_event_stream_error("AccessDeniedException"))
    assert isinstance(denied_mapped, UpstreamAuthFailed)


async def test_translate_stream_maps_mid_stream_bedrock_error() -> None:
    async def stream() -> Any:
        yield {"chunk": 1}
        raise _event_stream_error("ModelStreamErrorException")

    chunks = translate_stream(stream())
    assert await anext(chunks) == {"chunk": 1}
    with pytest.raises(UpstreamUnavailable):
        await anext(chunks)


# ── Fake boto3 bedrock-runtime client ────────────────────────────────────────


class _Body:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class FakeBedrockRuntime:
    last_client_kwargs: dict = {}
    last_kwargs: dict = {}

    def converse(self, **kwargs: Any) -> dict:
        FakeBedrockRuntime.last_kwargs = kwargs
        if (kwargs.get("toolConfig") or {}).get("toolChoice"):
            name = kwargs["toolConfig"]["toolChoice"]["tool"]["name"]
            content = [{"toolUse": {"toolUseId": "t1", "name": name, "input": {"answer": 42}}}]
            stop = "tool_use"
        else:
            content = [{"text": "hello from bedrock"}]
            stop = "end_turn"
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }

    def converse_stream(self, **kwargs: Any) -> dict:
        FakeBedrockRuntime.last_kwargs = kwargs
        events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "Hi"}, "contentBlockIndex": 0}},
            {"contentBlockDelta": {"delta": {"text": " there"}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 7, "totalTokens": 12}}},
        ]
        return {"stream": iter(events)}

    def invoke_model(self, **kwargs: Any) -> dict:
        FakeBedrockRuntime.last_kwargs = kwargs
        model_id = kwargs.get("modelId", "")
        body = json.loads(kwargs.get("body", "{}"))
        if model_id.startswith("amazon.titan-embed"):
            return {
                "body": _Body(
                    {"embedding": [0.1, 0.2, 0.3], "inputTextTokenCount": len(body["inputText"])}
                )
            }
        if model_id.startswith("cohere.embed"):
            return {"body": _Body({"embeddings": [[0.4, 0.5]] * len(body["texts"])})}
        if model_id.startswith("amazon.titan-image"):
            return {"body": _Body({"images": [base64.b64encode(b"PNG").decode("ascii")]})}
        raise AssertionError(f"unexpected modelId {model_id}")

    def close(self) -> None:
        return None


def _fake_boto3_client(service: str, **kwargs: Any) -> FakeBedrockRuntime:
    assert service == "bedrock-runtime"
    FakeBedrockRuntime.last_client_kwargs = kwargs
    return FakeBedrockRuntime()


def _patch_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bedrock_adapter.boto3, "client", _fake_boto3_client)
    # See conftest._patch: captured call args live on the class because boto3
    # constructs the client internally. Reset before every test so a test
    # that forgets to trigger the call under test fails on a KeyError instead
    # of silently reading a stale value left by the previous test.
    FakeBedrockRuntime.last_client_kwargs = {}
    FakeBedrockRuntime.last_kwargs = {}


# ── Dedicated executor (R6-M45) ──────────────────────────────────────────────


async def test_bedrock_blocking_calls_run_on_dedicated_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every blocking boto3 hop — non-streaming call, stream open, and each
    per-event pull — must run on the bounded Bedrock pool (identified by its
    thread-name prefix), not the loop's process-wide default executor."""
    from litestar_gateway.infrastructure.llm.bedrock_adapter import BedrockAdapter

    thread_names: list[str] = []

    def _record() -> None:
        thread_names.append(threading.current_thread().name)

    class RecordingBedrockRuntime(FakeBedrockRuntime):
        def converse(self, **kwargs: Any) -> dict:
            _record()
            return super().converse(**kwargs)

        def converse_stream(self, **kwargs: Any) -> dict:
            _record()
            events = super().converse_stream(**kwargs)["stream"]

            def recording_events() -> Any:
                for event in events:
                    _record()  # runs inside the per-event next() hop
                    yield event

            return {"stream": recording_events()}

    monkeypatch.setattr(
        bedrock_adapter.boto3, "client", lambda service, **kwargs: RecordingBedrockRuntime()
    )
    adapter = BedrockAdapter()
    request = {"messages": [{"role": "user", "content": "hi"}]}

    await adapter.achat_completion(request, _model(), BEDROCK_VALUES)
    stream = adapter.astream_chat_completion({**request, "stream": True}, _model(), BEDROCK_VALUES)
    async for _ in stream:
        pass

    assert thread_names, "expected blocking boto3 calls to be recorded"
    assert all(name.startswith("bedrock") for name in thread_names), thread_names


async def test_titan_embed_fanout_uses_dedicated_pool_separate_from_stream_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Titan embed fan-out must run on its own executor, not
    `_BEDROCK_EXECUTOR` — otherwise a large embed batch (>=8 texts) can occupy
    every worker for the duration of its network round trips, stalling
    concurrent chat streams' `_next_event` pulls, which share that pool
    (R7-M58)."""
    assert bedrock_adapter._BEDROCK_EMBED_EXECUTOR is not bedrock_adapter._BEDROCK_EXECUTOR

    thread_names: list[str] = []

    class RecordingTitanRuntime(_TrackingTitanRuntime):
        def invoke_model(self, **kwargs: Any) -> dict:
            thread_names.append(threading.current_thread().name)
            return super().invoke_model(**kwargs)

    runtime = RecordingTitanRuntime()
    _patch_titan(monkeypatch, runtime)
    texts = ["x" * (i + 1) for i in range(6)]
    await bedrock_adapter.BedrockAdapter().aembeddings(
        {"input": texts}, _model("amazon.titan-embed-text-v2:0"), BEDROCK_VALUES
    )

    assert thread_names, "expected embed invokes to be recorded"
    assert all(name.startswith("bedrock-embed") for name in thread_names), thread_names


# ── Integration through the endpoints ────────────────────────────────────────


async def test_bedrock_chat_completions(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(
        client,
        provider="bedrock",
        values=BEDROCK_VALUES,
        provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from bedrock"
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    # The client was built from the credential (region + keys), never the model.
    assert FakeBedrockRuntime.last_client_kwargs["region_name"] == "eu-west-1"
    assert FakeBedrockRuntime.last_client_kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
    assert FakeBedrockRuntime.last_kwargs["modelId"] == "anthropic.claude-3-5-sonnet-v2:0"
    # Client built with the gateway's resilience config (timeout + retries).
    boto_config = FakeBedrockRuntime.last_client_kwargs["config"]
    assert boto_config.read_timeout == DEFAULT_REQUEST_TIMEOUT
    assert boto_config.connect_timeout == DEFAULT_REQUEST_TIMEOUT
    assert boto_config.retries["max_attempts"] == DEFAULT_MAX_RETRIES


async def test_bedrock_structured_output_forces_a_tool(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(client, provider="bedrock", values=BEDROCK_VALUES)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            },
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    tool_config = FakeBedrockRuntime.last_kwargs["toolConfig"]
    assert tool_config["toolChoice"] == {"tool": {"name": "answer"}}
    choice = resp.json()["choices"][0]
    assert json.loads(choice["message"]["content"]) == {"answer": 42}
    assert choice["finish_reason"] == "stop"


async def test_bedrock_streaming_sse_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    key, team, admin = await _setup_team(client, provider="bedrock", values=BEDROCK_VALUES)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.text
    assert "chat.completion.chunk" in body
    assert "Hi" in body and "there" in body
    assert '"finish_reason": "stop"' in body
    assert "data: [DONE]" in body

    # The metadata event's usage is billed like every other provider stream.
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 5
    assert rows[0]["completion_tokens"] == 7


async def test_bedrock_responses_emulated(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(client, provider="bedrock", values=BEDROCK_VALUES)
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "instructions": "be brief", "input": "hi"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["object"] == "response"
    assert resp.json()["output_text"] == "hello from bedrock"
    # The Responses input was translated into Converse system + user message.
    assert FakeBedrockRuntime.last_kwargs["system"] == [{"text": "be brief"}]


async def test_bedrock_responses_emulated_streaming(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(client, provider="bedrock", values=BEDROCK_VALUES)
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.text
    assert "event: response.output_text.delta" in body
    assert "event: response.completed" in body
    assert "Hi" in body and "there" in body


async def test_bedrock_titan_embeddings(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(
        client,
        provider="bedrock",
        values=BEDROCK_VALUES,
        provider_model_id="amazon.titan-embed-text-v2:0",
        model_type="embeddings",
    )
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "m", "input": ["hello", "world"]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["object"] == "list"
    assert [d["index"] for d in body["data"]] == [0, 1]
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert body["usage"]["prompt_tokens"] == 10  # 5 + 5 chars via the fake's count


async def test_bedrock_cohere_embeddings(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(
        client,
        provider="bedrock",
        values=BEDROCK_VALUES,
        provider_model_id="cohere.embed-multilingual-v3",
        model_type="embeddings",
    )
    resp = await client.post(
        "/v1/embeddings",
        json={"model": "m", "input": "hello"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["data"][0]["embedding"] == [0.4, 0.5]


async def test_bedrock_titan_images(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(
        client,
        provider="bedrock",
        values=BEDROCK_VALUES,
        provider_model_id="amazon.titan-image-generator-v2:0",
        model_type="image",
    )
    resp = await client.post(
        "/v1/images/generations",
        json={"model": "m", "prompt": "a cat", "size": "512x512"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["data"][0]["b64_json"] == base64.b64encode(b"PNG").decode("ascii")
    body = json.loads(FakeBedrockRuntime.last_kwargs["body"])
    assert body["taskType"] == "TEXT_IMAGE"
    assert body["textToImageParams"]["text"] == "a cat"
    assert body["imageGenerationConfig"]["width"] == 512
    assert body["imageGenerationConfig"]["height"] == 512


async def test_bedrock_unknown_embedding_family_501(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(
        client,
        provider="bedrock",
        values=BEDROCK_VALUES,
        provider_model_id="meta.llama3-embeddings",  # no such family supported
        model_type="embeddings",
    )
    resp = await client.post(
        "/v1/embeddings", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == 501


async def test_bedrock_missing_region_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Creation-time validation would reject a region-less credential now, so
    # bypass it to simulate a legacy row created before validation existed:
    # the adapter must still yield a clean 400 at call time (defense in depth).
    from litestar_gateway.application import credential_service

    monkeypatch.setattr(credential_service, "validate_credential_values", lambda *_: None)
    _patch(monkeypatch)
    _patch_bedrock(monkeypatch)
    api_key = await _setup(
        client,
        provider="bedrock",
        values={"aws_access_key_id": "a", "aws_secret_access_key": "s"},  # pragma: allowlist secret
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


# ── Titan embeddings fan-out (unit) ──────────────────────────────────────────


class _TrackingTitanRuntime:
    """Fake bedrock-runtime whose invoke_model blocks briefly and records the
    maximum number of concurrent in-flight calls."""

    def __init__(self, fail_text: str | None = None) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0
        self._fail_text = fail_text
        self.max_in_flight = 0

    def invoke_model(self, **kwargs: Any) -> dict:
        body = json.loads(kwargs["body"])
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        time.sleep(0.02)
        try:
            if body["inputText"] == self._fail_text:
                raise RuntimeError("titan exploded")
            return {
                "body": _Body(
                    {"embedding": [float(len(body["inputText"]))], "inputTextTokenCount": 1}
                )
            }
        finally:
            with self._lock:
                self._in_flight -= 1

    def close(self) -> None:
        return None


def _patch_titan(monkeypatch: pytest.MonkeyPatch, runtime: _TrackingTitanRuntime) -> None:
    monkeypatch.setattr(bedrock_adapter.boto3, "client", lambda service, **kwargs: runtime)


async def test_titan_embeddings_fan_out_preserves_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TrackingTitanRuntime()
    _patch_titan(monkeypatch, runtime)
    texts = ["x" * (i + 1) for i in range(10)]
    body = await bedrock_adapter.BedrockAdapter().aembeddings(
        {"input": texts}, _model("amazon.titan-embed-text-v2:0"), BEDROCK_VALUES
    )
    assert [d["index"] for d in body["data"]] == list(range(10))
    # The fake embeds each text as [len(text)], so order is observable.
    assert [d["embedding"] for d in body["data"]] == [[float(i + 1)] for i in range(10)]
    assert body["usage"] == {"prompt_tokens": 10, "total_tokens": 10}
    assert runtime.max_in_flight >= 2  # calls actually overlapped


async def test_titan_embeddings_fan_out_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _TrackingTitanRuntime()
    _patch_titan(monkeypatch, runtime)
    texts = ["x" * (i + 1) for i in range(20)]
    await bedrock_adapter.BedrockAdapter().aembeddings(
        {"input": texts}, _model("amazon.titan-embed-text-v2:0"), BEDROCK_VALUES
    )
    assert 2 <= runtime.max_in_flight <= bedrock_adapter._TITAN_EMBED_FANOUT


async def test_titan_embeddings_fan_out_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TrackingTitanRuntime(fail_text="xxx")
    _patch_titan(monkeypatch, runtime)
    texts = ["x" * (i + 1) for i in range(6)]
    with pytest.raises(RuntimeError, match="titan exploded"):
        await bedrock_adapter.BedrockAdapter().aembeddings(
            {"input": texts}, _model("amazon.titan-embed-text-v2:0"), BEDROCK_VALUES
        )


def test_gateway_still_rejects_unregistered_providers() -> None:
    # Bedrock used to be the "unsupported provider" 501 fixture; now that every
    # enum value is registered, exercise the guard directly.
    from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl

    gateway = LLMGatewayImpl()
    gateway._registry.pop(Provider.BEDROCK)
    with pytest.raises(UnsupportedOperation):
        gateway._resolve(Provider.BEDROCK, "chat.completions")


async def test_gateway_native_messages_rejects_unsupported_provider_via_matrix() -> None:
    # R8-ISSUE-006: the gateway's own capability matrix — not just the
    # application-layer ProviderMismatch check — must gate the native
    # passthrough methods. Bedrock has no `anative_messages`/`agenerate_content`
    # on its adapter, so bypassing the matrix would surface an unguarded
    # AttributeError (-> opaque 500) instead of a clean UnsupportedOperation
    # (-> 501).
    from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl

    gateway = LLMGatewayImpl()
    model = _model("anthropic.claude-3-5-sonnet-v2:0")
    with pytest.raises(UnsupportedOperation):
        await gateway.anative_messages({}, model, BEDROCK_VALUES)
    with pytest.raises(UnsupportedOperation):
        await gateway.astream_native_messages({}, model, BEDROCK_VALUES)
    with pytest.raises(UnsupportedOperation):
        await gateway.agenerate_content({}, model, BEDROCK_VALUES)
    with pytest.raises(UnsupportedOperation):
        await gateway.astream_generate_content({}, model, BEDROCK_VALUES)


async def test_gateway_native_capability_slots_still_resolve_for_supported_providers() -> None:
    # Anthropic/Vertex must keep working through the matrix now that native
    # passthrough is a real, gated capability rather than an implicit bypass.
    from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl

    gateway = LLMGatewayImpl()
    anthropic_adapter = gateway._resolve(Provider.ANTHROPIC, "native.messages")
    assert hasattr(anthropic_adapter, "anative_messages")
    vertex_adapter = gateway._resolve(Provider.VERTEX_AI, "native.generate_content")
    assert hasattr(vertex_adapter, "agenerate_content")
