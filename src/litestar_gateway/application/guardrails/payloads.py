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
    turn = _last_gemini_turn(request)
    if turn is not None:
        return _parts_text(turn)
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
    # Anthropic Messages answers in top-level `content` blocks, Gemini in
    # `candidates[].content.parts[]`. Neither uses `choices`, so without these
    # the native surface handed every response chain an empty string — which
    # reads as "nothing to object to" and allowed everything through.
    parts.append(_content_text(response.get("content")))
    for candidate in response.get("candidates", []) or []:
        if isinstance(candidate, dict):
            parts.append(_parts_text(candidate.get("content")))
    return "\n".join(p for p in parts if p)


def can_redact_request(request: dict[str, Any]) -> bool:
    message = _last_user_message(request)
    if message is not None:
        return isinstance(message.get("content"), str)
    if isinstance(request.get("input"), str):
        return True
    turn = _last_gemini_turn(request)
    if turn is not None:
        # One text part is an unambiguous target; several are not, and guessing
        # which one the flattened text belongs to is the guess this module
        # refuses to make.
        return len(_text_parts(turn)) == 1
    return False


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
    turn = _last_gemini_turn(updated)
    if turn is not None:
        texts = _text_parts(turn)
        if len(texts) == 1:
            texts[0]["text"] = text
    return updated


def can_redact_response(response: dict[str, Any]) -> bool:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        return all(
            isinstance(c, dict)
            and isinstance(c.get("message"), dict)
            and isinstance(c["message"].get("content"), str)
            for c in choices
        )
    # Anthropic: a single text block. More than one block means the answer also
    # carries something that is not text (a tool_use), and the flattened verdict
    # cannot be mapped back onto it.
    blocks = response.get("content")
    if isinstance(blocks, list):
        return (
            len(blocks) == 1
            and isinstance(blocks[0], dict)
            and isinstance(blocks[0].get("text"), str)
        )
    candidates = response.get("candidates")
    if isinstance(candidates, list) and len(candidates) == 1:
        turn = candidates[0].get("content") if isinstance(candidates[0], dict) else None
        return isinstance(turn, dict) and len(_text_parts(turn)) == 1
    return False


def redact_response(response: dict[str, Any], text: str) -> dict[str, Any]:
    """A copy of `response` with every choice's content replaced.

    Every choice gets the same redacted text because the verdict was taken on
    their concatenation — splitting it back per choice would be invention.
    """
    updated = deepcopy(response)
    choices = updated.get("choices")
    if isinstance(choices, list) and choices:
        for choice in choices:
            choice["message"]["content"] = text
        return updated
    blocks = updated.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block["text"] = text
        return updated
    for candidate in updated.get("candidates", []) or []:
        turn = candidate.get("content") if isinstance(candidate, dict) else None
        if isinstance(turn, dict):
            for part in _text_parts(turn):
                part["text"] = text
    return updated


def _last_user_message(request: dict[str, Any]) -> dict[str, Any] | None:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return message
    return None


def _last_gemini_turn(request: dict[str, Any]) -> dict[str, Any] | None:
    """Gemini keeps the conversation in `contents`, each turn a list of `parts`.

    `role` may be omitted, and when it is the turn is the user's — so an absent
    role must not make a turn invisible to the guardrail.
    """
    contents = request.get("contents")
    if not isinstance(contents, list):
        return None
    for turn in reversed(contents):
        if isinstance(turn, dict) and turn.get("role", "user") == "user":
            return turn
    return None


def _text_parts(turn: Any) -> list[dict[str, Any]]:
    """The text-carrying parts of one Gemini turn, as live references so a
    caller holding a copy can rewrite them in place."""
    if not isinstance(turn, dict):
        return []
    parts = turn.get("parts")
    if not isinstance(parts, list):
        return []
    return [p for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]


def _parts_text(turn: Any) -> str:
    return "\n".join(part["text"] for part in _text_parts(turn))


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
