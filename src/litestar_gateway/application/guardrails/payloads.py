"""Turning a request or a response into something a guardrail can judge — and
putting a redaction back.

Extraction is the easy half. Re-insertion is not: a chat request is a list of
messages whose content may be a string or a list of multimodal blocks, and
"here is the redacted flat text" does not say which block each piece came from.
Guessing would be worse than refusing, so a redaction is applied only where the
mapping is unambiguous — a single string content — and a REDACT verdict on a
shape we cannot rewrite is escalated to a BLOCK rather than passed through
unredacted. Failing closed is the only safe direction for a control that exists
to keep content from leaving.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def request_text(request: dict[str, Any]) -> str:
    """What the caller asked, flattened for inspection.

    The last user turn, because that is the new content this call introduces:
    earlier turns were already judged when they were sent, and re-judging the
    whole transcript on every follow-up would block a conversation retroactively
    on content the operator already allowed through.
    """
    message = _last_user_message(request)
    if message is not None:
        return _content_text(message.get("content"))
    value = request.get("input")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            _content_text(item.get("content")) for item in value if isinstance(item, dict)
        )
    return ""


def response_text(response: dict[str, Any]) -> str:
    """What the model answered, flattened for inspection."""
    parts: list[str] = []
    for choice in response.get("choices", []) or []:
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                parts.append(_content_text(message.get("content")))
    for item in response.get("output", []) or []:
        if isinstance(item, dict):
            parts.append(_content_text(item.get("content")))
    return "\n".join(p for p in parts if p)


def can_redact_request(request: dict[str, Any]) -> bool:
    message = _last_user_message(request)
    if message is not None:
        return isinstance(message.get("content"), str)
    return isinstance(request.get("input"), str)


def redact_request(request: dict[str, Any], text: str) -> dict[str, Any]:
    """A copy of `request` with the judged text replaced.

    Never mutates the caller's dict: the original is what the trace and the
    error path may still need to describe.
    """
    updated = deepcopy(request)
    message = _last_user_message(updated)
    if message is not None:
        message["content"] = text
        return updated
    if isinstance(updated.get("input"), str):
        updated["input"] = text
    return updated


def can_redact_response(response: dict[str, Any]) -> bool:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    return all(
        isinstance(c, dict)
        and isinstance(c.get("message"), dict)
        and isinstance(c["message"].get("content"), str)
        for c in choices
    )


def redact_response(response: dict[str, Any], text: str) -> dict[str, Any]:
    """A copy of `response` with every choice's content replaced.

    Every choice gets the same redacted text because the verdict was taken on
    their concatenation — splitting it back per choice would be invention.
    """
    updated = deepcopy(response)
    for choice in updated.get("choices", []):
        choice["message"]["content"] = text
    return updated


def _last_user_message(request: dict[str, Any]) -> dict[str, Any] | None:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return message
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""
