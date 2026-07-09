"""Contract guard for ISSUE-005: the native Gemini passthrough
(`VertexAdapter.agenerate_content` / `astream_generate_content`, via the
`_raw_request` / `_raw_request_streamed` helpers in `vertex_adapter.py`) calls two
**private** `google-genai` methods — `client.aio._api_client.async_request` and
`async_request_streamed` — because the SDK has no public raw-request API.

`pyproject.toml` caps `google-genai<3`, but a minor/patch bump within that range
could still rename or re-sign these private methods. Everywhere else in the test
suite, the Gemini client is a hand-written fake (`FakeGenaiClient` /
`_FakeGeminiApiClient`) that hardcodes this exact private shape, so it cannot
detect a real SDK regression. This test constructs a REAL `genai.Client` (offline —
`api_key="x"`, no network call is made) against whichever `google-genai` version is
actually installed, and asserts the private surface the adapter depends on is still
there with the expected signature. If the installed SDK ever breaks this, this test
fails in CI with a clear message instead of the passthrough 500ing in production.
"""

from __future__ import annotations

import inspect

from google import genai

_EXPECTED_PARAMS = ("http_method", "path", "request_dict")


def _assert_callable_with_expected_params(obj: object, name: str) -> None:
    method = getattr(obj, name, None)
    assert method is not None and callable(method), (
        f"google-genai's private `client.aio._api_client.{name}` is gone or no longer "
        "callable. The native Gemini passthrough in vertex_adapter.py "
        "(_raw_request / _raw_request_streamed) depends on this private method; an "
        "SDK upgrade removed/renamed it. Pin google-genai to the last known-good "
        "version and re-check the ISSUE-005 wrapper before relaxing the pin."
    )
    params = list(inspect.signature(method).parameters)
    for expected in _EXPECTED_PARAMS:
        assert expected in params, (
            f"google-genai's private `client.aio._api_client.{name}` no longer "
            f"accepts a `{expected}` parameter (got {params!r}). The native Gemini "
            "passthrough in vertex_adapter.py builds this call positionally as "
            "(http_method, path, request_dict) — an SDK upgrade changed the private "
            "signature. Update _raw_request/_raw_request_streamed (and this test) "
            "to match, or re-pin google-genai."
        )


def test_private_raw_request_contract_holds_on_installed_sdk() -> None:
    # Offline construction only: no network call, no real credentials needed.
    client = genai.Client(api_key="x")
    api_client = client.aio._api_client
    _assert_callable_with_expected_params(api_client, "async_request")
    _assert_callable_with_expected_params(api_client, "async_request_streamed")
