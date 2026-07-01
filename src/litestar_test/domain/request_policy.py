"""Deny-by-default sanitizing of a client request before it hits a provider SDK.

The gateway splats the client's OpenAI-shaped body into the SDK call, so without
a policy a tenant could pass SDK-special kwargs (``extra_headers``,
``extra_body``, ``extra_query``, ``timeout`` …) to manipulate how we call the
upstream with *our* credential, or inflate cost with an unbounded ``n`` /
``max_tokens``. This keeps only an explicit allowlist per operation and clamps
the cost-driving numbers. It is a pure function — no I/O.

Only the (untrusted) client request is sanitized; ``model.params`` is trusted
admin/team-admin config and is merged separately by the adapters.
"""

from __future__ import annotations

from typing import Any

# Accepted fields per operation. Anything else (including transport overrides
# like extra_headers/extra_body/extra_query/timeout/api_key) is dropped.
_ALLOWED: dict[str, frozenset[str]] = {
    "chat.completions": frozenset(
        {
            "model",
            "messages",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "n",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "logprobs",
            "top_logprobs",
            "response_format",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "seed",
            "stream",
            "stream_options",
            "reasoning_effort",
            "user",
        }
    ),
    "responses": frozenset(
        {
            "model",
            "input",
            "instructions",
            "max_output_tokens",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "text",
            "reasoning",
            "metadata",
            "store",
            "previous_response_id",
            "stream",
            "user",
        }
    ),
    "embeddings": frozenset({"model", "input", "dimensions", "encoding_format", "user"}),
    "images": frozenset(
        {
            "model",
            "prompt",
            "size",
            "quality",
            "style",
            "n",
            "response_format",
            "background",
            "output_format",
            "user",
        }
    ),
}

# Ceilings applied to client-provided values (trusted admin params are not capped).
MAX_N = 8
MAX_TOKENS = 32_000
_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def _clamp_int(value: Any, ceiling: int) -> Any:
    # bool is an int subclass; leave non-ints for the provider to validate.
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return min(value, ceiling)


def sanitize_request(operation: str, request: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `request` with only the allowlisted fields for
    `operation`, cost-driving numbers clamped. Unknown operations pass through."""
    allowed = _ALLOWED.get(operation)
    if allowed is None:  # pragma: no cover - defensive; callers pass known ops
        return dict(request)

    cleaned = {key: value for key, value in request.items() if key in allowed}
    if "n" in cleaned:
        cleaned["n"] = _clamp_int(cleaned["n"], MAX_N)
    for field in _TOKEN_FIELDS:
        if field in cleaned:
            cleaned[field] = _clamp_int(cleaned[field], MAX_TOKENS)
    return cleaned
