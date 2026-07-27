"""Response-cache key derivation and cacheability (Plan 04 Phase 0, design §2/§7).

Pure functions, no I/O — the single most important (and easiest to regress)
piece of the response cache, so it gets its own module and its own
table-driven unit tests.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.ports.response_cache import CacheKey

# Only these two operations participate in Phase 0 (non-streamed only — the
# streaming endpoints never call into this). Embeddings/images/native bypass.
CACHEABLE_OPERATIONS = frozenset({"chat.completions", "responses"})

# Fields that provably cannot change the generated content: everything else in
# the request is part of the key. A deny-list (not an allow-list) is what makes
# a field the gateway starts accepting later cacheable-safe by default —
# ISSUE-023 was an allow-list silently ignoring `n`, `parallel_tool_calls`,
# penalties and reasoning options, so a request that differed only in those
# received another request's answer.
_NON_DETERMINING_FIELDS = frozenset(
    {
        # The name the client called: an alias and the model it resolves to must
        # share a hit, and the resolved identity is in the key explicitly below.
        "model",
        "stream",  # transport, not content (a cached body is replayed as a stream)
        "stream_options",
        "user",  # end-user attribution label
        "metadata",  # caller-defined tags, echoed not interpreted
    }
)

# Bumped whenever the derivation changes. Entries written by an older gateway
# then simply never match, instead of matching under different semantics.
_KEY_SCHEMA_VERSION = "v2"


def canonical_view(
    model: Model, operation: str, effective_request: dict[str, Any]
) -> dict[str, Any]:
    """The pure, order-insensitive description of "which request is this" —
    resolved model identity, operation, and every determining request field.
    Shared by the exact-match key and the semantic scope (which hashes this
    same view minus the text it embeds), so the two tiers can never disagree
    about what counts as the same request."""
    return {
        "v": _KEY_SCHEMA_VERSION,
        "operation": operation,
        "model_id": str(model.id),
        "model_name": model.name,
        "provider": model.provider.value,
        "provider_model_id": model.provider_model_id,
        "api_version": model.api_version,
        "max_output_tokens": model.max_output_tokens,
        "request": {
            field: value
            for field, value in effective_request.items()
            if field not in _NON_DETERMINING_FIELDS
        },
    }


def digest_of(view: dict[str, Any]) -> str:
    """`json.dumps(sort_keys=True)` canonicalizes object-key order at every
    nesting level without touching string content, so semantically-significant
    whitespace inside message text is preserved verbatim."""
    return hashlib.sha256(
        json.dumps(view, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def derive_cache_key(
    team_id: UUID,
    api_key_id: UUID | None,
    model: Model,
    operation: str,
    effective_request: dict[str, Any],
) -> CacheKey:
    """A pure, canonical view of *what will actually be sent*, hashed.

    `effective_request` must be the post-merge request (`Model.merge_params`),
    so admin defaults and enforced policy are part of the key: a policy change
    is a different request, not a silent hit on the pre-change answer. The
    model is identified by `id` as well as name — a delete/recreate under the
    same name, or two models sharing a name across scopes, must not share
    entries — plus the provider-side coordinates that select the upstream
    deployment. `operation` separates chat from Responses, whose bodies differ
    in shape for the same text.

    Object-key order never affects the result; string content is untouched.
    """
    view = canonical_view(model, operation, effective_request)
    return CacheKey(team_id=team_id, api_key_id=api_key_id, digest=digest_of(view))


def is_cacheable(operation: str, request: dict[str, Any], model: Model) -> bool:
    """Whether this request may look up / write the response cache at all —
    the global kill-switch and streaming are gated by the caller; this checks
    everything else (design §7): only chat.completions/responses participate,
    the team+model must have opted in, and a sampled (`temperature > 0`)
    request is refused unless the model explicitly allows non-determinism.
    Stateful Responses threads (`store`) are excluded upstream already —
    `sanitize_request`/`validate_responses_request` reject anything but
    `store=False`/absent before the request ever reaches here."""
    if operation not in CACHEABLE_OPERATIONS:
        return False
    if not model.cache_enabled:
        return False
    temperature = request.get("temperature")
    if (
        isinstance(temperature, int | float)
        and not isinstance(temperature, bool)
        and temperature > 0
        and not model.cache_allow_nondeterministic
    ):
        return False
    return True
