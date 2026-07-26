"""Semantic-tier eligibility, text extraction, and similarity (Plan 04 Phase 2).

Pure functions, no I/O — mirrors `domain/response_cache_key.py`'s separation
of pure logic from the adapters that store/embed. `cosine_similarity` mirrors
`application/routing/embeddings.py`'s `_cosine` verbatim (design §1); it is
duplicated rather than imported because that module is `application/`-layer
and this one must stay dependency-free `domain/`.
"""

from __future__ import annotations

import math
from typing import Any

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.response_cache_key import is_cacheable


def is_semantic_cacheable(operation: str, request: dict[str, Any], model: Model) -> bool:
    """Whether this request may fall back to the semantic tier on an
    exact-match miss (design §1/§7): the semantic tier is never built without
    the exact-match tier in front of it, so every exact-match eligibility rule
    (`is_cacheable`) applies first, plus the model's *separate* semantic
    opt-in (`cache_semantic_enabled`) — exact-match may be on while semantic
    stays off."""
    return is_cacheable(operation, request, model) and model.cache_semantic_enabled


def _text_from_content(content: Any) -> str:
    """A plain-text view of a message's `content`: verbatim for a string,
    joined text parts for a multimodal block list (non-text blocks such as
    images contribute nothing — they cannot be meaningfully embedded here)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def extract_semantic_text(request: dict[str, Any]) -> str | None:
    """Best-effort plain-text view of the request, for embedding.

    Concatenates `messages`/`input` content in order. Returns `None` when
    there is no extractable text (e.g. an all-image request) — the caller
    treats that as semantic-ineligible for this request rather than embedding
    an empty string."""
    for field in ("messages", "input"):
        value = request.get(field)
        if isinstance(value, str):
            return value or None
        if isinstance(value, list):
            parts = [
                _text_from_content(item.get("content")) for item in value if isinstance(item, dict)
            ]
            text = "\n".join(part for part in parts if part)
            return text or None
    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors; 0.0 when either norm is zero."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
