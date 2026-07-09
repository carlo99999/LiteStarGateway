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

from litestar_gateway.domain.entities.enums import Provider
from litestar_gateway.domain.exceptions import UnsupportedNativeField

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

# Canonical output-token field to inject per operation when a per-model ceiling
# is set but the client sent none. Operations without an output-token concept
# (embeddings, images) are absent and get no injection.
_OUTPUT_TOKEN_FIELD = {
    "chat.completions": "max_tokens",
    "responses": "max_output_tokens",
}


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


# --- Native passthrough governance ---------------------------------------------
#
# The native surfaces forward the client's provider-shaped body verbatim (no
# `sanitize_request`), so the guards the OpenAI surface gets for free must be
# reapplied here on the two governance concerns that touch security/money: the
# reserved SDK control kwargs (credential-override vector) and the output-token
# ceiling. Everything else about the body stays untouched.

# SDK control kwargs the client SDKs treat as transport params, not request
# fields. Splatting them into `messages.create(**body)` lets a tenant override
# the vaulted credential (`extra_headers={"x-api-key": ...}`) or inject outbound
# transport options — so they are rejected on the native surface (leading-
# underscore keys are private SDK params and are rejected too).
_NATIVE_CONTROL_KWARGS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})

# The native output-token field per provider, and how it nests. Anthropic Messages
# carries `max_tokens` top-level; Gemini nests `maxOutputTokens` under
# `generationConfig`. Keyed on the Provider enum so this stays pure request-shape
# policy with no infra dependency.
_NATIVE_OUTPUT_FIELD: dict[Provider, str] = {
    Provider.ANTHROPIC: "max_tokens",
    Provider.VERTEX_AI: "maxOutputTokens",
}


def reject_native_control_kwargs(body: dict[str, Any]) -> None:
    """Reject a native body carrying SDK control kwargs or leading-underscore keys.

    Prefer rejecting over silently stripping so the client learns the field is not
    forwarded (it would otherwise think its override took effect). Provider-agnostic
    — applied to every native request as defense in depth for both surfaces."""
    bad = sorted(k for k in body if k in _NATIVE_CONTROL_KWARGS or k.startswith("_"))
    if bad:
        raise UnsupportedNativeField(f"fields not allowed on the native surface: {bad}")


def _native_effective_ceiling(model_ceiling: int | None) -> int:
    """The output-token ceiling to enforce on a native body: the per-model
    `max_output_tokens` when set, always bounded by the global `MAX_TOKENS`."""
    return min(MAX_TOKENS, model_ceiling) if model_ceiling is not None else MAX_TOKENS


def clamp_native_output_tokens(
    provider: Provider, body: dict[str, Any], model_ceiling: int | None
) -> dict[str, Any]:
    """Enforce the output-token ceiling on a native body's provider-specific field
    (`min(client value, model ceiling, global MAX_TOKENS)`), mirroring the OpenAI
    surface's `clamp_output_tokens`/`sanitize_request`. A present value is clamped
    down; if the client omitted it and a per-model ceiling is set, the field is
    injected at the ceiling so omission cannot bypass the cap. Returns a copy;
    the rest of the body is left verbatim. Unknown providers pass through."""
    field = _NATIVE_OUTPUT_FIELD.get(provider)
    if field is None:  # pragma: no cover - native surfaces are Anthropic/Vertex only
        return body
    ceiling = _native_effective_ceiling(model_ceiling)
    governed = dict(body)
    if provider is Provider.VERTEX_AI:
        config = dict(governed.get("generationConfig") or {})
        value = config.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            config[field] = min(value, ceiling)
            governed["generationConfig"] = config
        elif model_ceiling is not None:
            config[field] = ceiling
            governed["generationConfig"] = config
        return governed
    value = governed.get(field)
    if isinstance(value, int) and not isinstance(value, bool):
        governed[field] = min(value, ceiling)
    elif model_ceiling is not None:
        governed[field] = ceiling
    return governed


def native_reservation_view(provider: Provider, body: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-shaped view of a native body for budget admission + H14 estimation.

    `_reservation_cost`/`_request_text`/`_max_output_tokens` read the OpenAI keys
    (`messages`/`max_tokens`/`n`); the Anthropic Messages body already uses those,
    but a Gemini body carries the prompt under `contents[].parts[].text`, the
    output ceiling under `generationConfig.maxOutputTokens`, and the choice count
    under `generationConfig.candidateCount`. Map the Gemini shape so admission
    reserves the real pessimistic cost — the inverse of `_gemini_usage` at
    settlement. Anthropic passes through unchanged."""
    if provider is not Provider.VERTEX_AI:
        return body
    texts = [
        part["text"]
        for content in body.get("contents") or []
        if isinstance(content, dict)
        for part in content.get("parts") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    config = body.get("generationConfig") or {}
    return {
        "messages": [{"content": "\n".join(texts)}],
        "max_tokens": config.get("maxOutputTokens") or 0,
        "n": config.get("candidateCount") or 1,
    }


def clamp_output_tokens(
    operation: str, request: dict[str, Any], ceiling: int | None
) -> dict[str, Any]:
    """Enforce a per-model output-token `ceiling` with `min` (clamp) semantics.

    Any output-token field the client sent is lowered to `min(value, ceiling)`;
    if the client sent none, the operation's canonical field is injected at the
    ceiling so omission cannot bypass the cap. A `None` ceiling (the default for
    every model) is a no-op — the request passes through unchanged. Returns a
    copy; never mutates the input. Runs after `sanitize_request`, once the model
    is resolved, so the reservation and the provider call see the same numbers."""
    if ceiling is None:
        return request
    cleaned = dict(request)
    present = [field for field in _TOKEN_FIELDS if field in cleaned]
    for field in present:
        cleaned[field] = _clamp_int(cleaned[field], ceiling)
    if not present:
        field = _OUTPUT_TOKEN_FIELD.get(operation)
        if field is not None:
            cleaned[field] = ceiling
    return cleaned
