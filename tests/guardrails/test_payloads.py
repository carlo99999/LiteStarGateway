"""Extraction and re-insertion around the judged text.

The interesting cases are all about shapes we must NOT rewrite: the module's
contract is that it either applies a redaction exactly or admits it cannot, and
the caller escalates to a block. A silent partial rewrite would be the one
outcome worse than either.
"""

from __future__ import annotations

from litestar_gateway.application.guardrails.payloads import (
    can_redact_request,
    can_redact_response,
    redact_request,
    redact_response,
    request_text,
    response_text,
)

# ── Request extraction ────────────────────────────────────────────────────────


def test_request_text_is_the_last_user_turn() -> None:
    # Not the whole transcript: earlier turns were judged when they were sent,
    # and re-judging them would retroactively block an allowed conversation.
    request = {
        "messages": [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "sure"},
            {"role": "user", "content": "second"},
        ]
    }
    assert request_text(request) == "second"


def test_request_text_flattens_multimodal_text_blocks() -> None:
    request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    {"type": "text", "text": "this"},
                ],
            }
        ]
    }
    # The image is not judged — only text the provider can reason about is.
    assert request_text(request) == "look at\nthis"


def test_request_text_reads_the_responses_api_input() -> None:
    assert request_text({"input": "a prompt"}) == "a prompt"
    assert (
        request_text(
            {"input": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]}
        )
        == "one\ntwo"
    )


def test_request_text_of_an_unknown_shape_is_empty_not_an_error() -> None:
    # An empty string means "nothing to judge", which every provider treats as
    # ALLOW. A raise here would turn an odd body into a 500.
    assert request_text({}) == ""
    assert request_text({"messages": "not a list"}) == ""
    assert request_text({"messages": [{"role": "assistant", "content": "hi"}]}) == ""


# ── Response extraction ───────────────────────────────────────────────────────


def test_response_text_joins_every_choice() -> None:
    response = {
        "choices": [
            {"message": {"content": "first"}},
            {"message": {"content": "second"}},
        ]
    }
    assert response_text(response) == "first\nsecond"


def test_response_text_reads_responses_api_output() -> None:
    response = {"output": [{"content": [{"type": "output_text", "text": "answer"}]}]}
    assert response_text(response) == "answer"


def test_response_text_of_a_toolcall_only_answer_is_empty() -> None:
    # content is null when the model only emitted tool calls: nothing to judge.
    assert response_text({"choices": [{"message": {"content": None, "tool_calls": []}}]}) == ""


# ── Redaction: applied exactly, or refused ────────────────────────────────────


def test_redact_request_rewrites_the_last_user_turn_only() -> None:
    request = {
        "messages": [
            {"role": "user", "content": "old one"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "my ssn is 1234"},
        ]
    }
    assert can_redact_request(request)
    updated = redact_request(request, "my ssn is [REDACTED]")

    assert updated["messages"][2]["content"] == "my ssn is [REDACTED]"
    assert updated["messages"][0]["content"] == "old one"
    # The caller's dict is untouched — the trace and the error path may still
    # need to describe the request as it arrived.
    assert request["messages"][2]["content"] == "my ssn is 1234"


def test_multimodal_request_cannot_be_redacted() -> None:
    # Flat text does not say which block each piece came from, so re-inserting
    # it would be a guess. The caller turns this into a block.
    request = {"messages": [{"role": "user", "content": [{"type": "text", "text": "secret"}]}]}
    assert not can_redact_request(request)


def test_request_with_no_user_turn_and_no_string_input_cannot_be_redacted() -> None:
    assert not can_redact_request({"messages": [{"role": "system", "content": "x"}]})
    assert not can_redact_request({"input": [{"role": "user", "content": "x"}]})
    assert can_redact_request({"input": "x"})


def test_redact_response_replaces_every_choice() -> None:
    response = {"choices": [{"message": {"content": "a"}}, {"message": {"content": "b"}}]}
    assert can_redact_response(response)
    updated = redact_response(response, "[REDACTED]")

    # Every choice gets the same text: the verdict was taken on their
    # concatenation, and splitting it back per choice would be invention.
    assert [c["message"]["content"] for c in updated["choices"]] == ["[REDACTED]", "[REDACTED]"]
    assert response["choices"][0]["message"]["content"] == "a"


def test_response_without_string_content_cannot_be_redacted() -> None:
    assert not can_redact_response({"choices": []})
    assert not can_redact_response({"choices": [{"message": {"content": None}}]})
    assert not can_redact_response({"output": [{"content": "x"}]})
    # One unrewritable choice is enough to refuse the whole response: a partial
    # redaction would ship the unredacted half.
    assert not can_redact_response(
        {"choices": [{"message": {"content": "ok"}}, {"message": {"content": None}}]}
    )


# ── Provider-native shapes (ISSUE-038) ────────────────────────────────────────
#
# The native passthrough endpoints speak their vendor's protocol, not OpenAI's.
# Anthropic Messages reuses `messages`, so the request side was already covered;
# Gemini carries `contents`/`parts`, and neither vendor answers in `choices`, so
# the response side judged an empty string and allowed everything through.


def test_request_text_reads_a_gemini_contents_turn() -> None:
    body = {"contents": [{"role": "user", "parts": [{"text": "my ssn is 1234"}]}]}

    assert request_text(body) == "my ssn is 1234"


def test_request_text_reads_the_last_gemini_turn_only() -> None:
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": "old"}]},
            {"role": "model", "parts": [{"text": "answer"}]},
            {"role": "user", "parts": [{"text": "new"}]},
        ]
    }

    assert request_text(body) == "new"


def test_request_text_reads_a_gemini_turn_without_an_explicit_role() -> None:
    # The protocol lets `role` be omitted, and it means user.
    assert request_text({"contents": [{"parts": [{"text": "hi"}]}]}) == "hi"


def test_redact_request_rewrites_a_gemini_turn() -> None:
    body = {"contents": [{"role": "user", "parts": [{"text": "ssn 1234"}]}]}

    assert can_redact_request(body)
    redacted = redact_request(body, "ssn [MASKED]")

    assert redacted["contents"][0]["parts"][0]["text"] == "ssn [MASKED]"
    # The caller's dict is never mutated.
    assert body["contents"][0]["parts"][0]["text"] == "ssn 1234"


def test_a_gemini_turn_with_several_parts_cannot_be_redacted() -> None:
    # Which part did each piece of the flattened text come from? Unanswerable,
    # so this escalates to a block rather than a guess.
    body = {"contents": [{"role": "user", "parts": [{"text": "a"}, {"text": "b"}]}]}

    assert not can_redact_request(body)


def test_response_text_reads_anthropic_content_blocks() -> None:
    body = {"content": [{"type": "text", "text": "hi there"}], "usage": {"input_tokens": 1}}

    assert response_text(body) == "hi there"


def test_response_text_reads_gemini_candidates() -> None:
    body = {"candidates": [{"content": {"parts": [{"text": "ciao"}]}}]}

    assert response_text(body) == "ciao"


def test_redact_response_rewrites_an_anthropic_text_block() -> None:
    body = {"content": [{"type": "text", "text": "secret"}]}

    assert can_redact_response(body)
    assert redact_response(body, "[MASKED]")["content"][0]["text"] == "[MASKED]"


def test_redact_response_rewrites_a_gemini_candidate() -> None:
    body = {"candidates": [{"content": {"parts": [{"text": "secret"}]}}]}

    assert can_redact_response(body)
    redacted = redact_response(body, "[MASKED]")

    assert redacted["candidates"][0]["content"]["parts"][0]["text"] == "[MASKED]"


def test_an_anthropic_answer_with_a_toolcall_block_cannot_be_redacted() -> None:
    body = {"content": [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "t"}]}

    assert not can_redact_response(body)
