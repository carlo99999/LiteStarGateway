"""Guard the text-in/text-out chat translators against request features they
cannot express.

The Vertex and Bedrock chat translators handle text messages plus structured
output (`response_format`) but not real tool/function calling. Anthropic also
handles the governed non-streaming tool subset. None translates non-text (e.g.
image) message content. Features outside those subsets must fail loudly with
`UnsupportedOperation` (→ 501), never be silently dropped.

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


def ensure_translatable_chat_request(
    request: dict[str, Any], provider: str, *, allow_tools: bool = False
) -> None:
    """Reject chat-request features these text-only translators would drop.

    `request` is the effective (post-`merge_params`) request. Raises
    `UnsupportedOperation` for tool/function calling or non-text content; returns
    None otherwise."""
    has_tool_messages = any(
        isinstance(message, dict)
        and (message.get("role") in {"tool", "function"} or message.get("tool_calls") is not None)
        for message in request.get("messages") or []
    )
    if not allow_tools and (
        request.get("tools") is not None
        or request.get("tool_choice") is not None
        or request.get("parallel_tool_calls") is not None
        or has_tool_messages
    ):
        raise UnsupportedOperation(
            f"Provider '{provider}' does not support tool/function calling; "
            "route this request to an OpenAI or Azure model."
        )
    if _has_non_text_content(request):
        raise UnsupportedOperation(
            f"Provider '{provider}' does not support non-text (e.g. image) message "
            "content; route this request to an OpenAI or Azure model."
        )
