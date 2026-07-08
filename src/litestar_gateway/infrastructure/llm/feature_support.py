"""Guard the text-in/text-out chat translators against request features they
cannot express.

The Anthropic, Vertex and Bedrock chat translators handle text messages plus
structured output (`response_format`). They do **not** translate real tool /
function calling (`tools`/`tool_choice`) or non-text (e.g. image) message
content — those get silently dropped, so the model would answer a
quietly-stripped request and the client would never know its feature was
ignored. Rather than lie, fail loudly with `UnsupportedOperation` (→ 501) so the
caller routes such requests to a provider that supports them (OpenAI/Azure).

Structured output is intentionally *not* rejected here: it arrives via
`response_format`, not `tools`, and each translator maps it natively.
"""

from __future__ import annotations

from typing import Any

from litestar_gateway.domain.exceptions import UnsupportedOperation


def _has_non_text_content(request: dict[str, Any]) -> bool:
    """True if any message carries a non-text content part (image_url, audio, …).
    A content part is text when it is absent (plain-string content) or a dict
    whose `type` is `"text"`; anything else is a modality we don't translate."""
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") not in (None, "text"):
                    return True
    return False


def ensure_translatable_chat_request(request: dict[str, Any], provider: str) -> None:
    """Reject chat-request features these text-only translators would drop.

    `request` is the effective (post-`merge_params`) request. Raises
    `UnsupportedOperation` for tool/function calling or non-text content; returns
    None otherwise."""
    if request.get("tools") is not None or request.get("tool_choice") is not None:
        raise UnsupportedOperation(
            f"Provider '{provider}' does not support tool/function calling; "
            "route this request to an OpenAI or Azure model."
        )
    if _has_non_text_content(request):
        raise UnsupportedOperation(
            f"Provider '{provider}' does not support non-text (e.g. image) message "
            "content; route this request to an OpenAI or Azure model."
        )
