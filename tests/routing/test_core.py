"""Smart routing: S1 complexity classification, capability filters, context extraction."""

from __future__ import annotations

import pytest

from litestar_gateway.application.routing.complexity import ComplexityStrategy
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RoutingContext,
    build_routing_context,
    filter_candidates,
    nearest_tier_candidate,
)


def _candidate(name: str, tier: QualityTier, **kwargs) -> CandidateModel:
    return CandidateModel(model_name=name, description=f"{name} desc", quality_tier=tier, **kwargs)


CANDIDATES = (
    _candidate("cheap", QualityTier.SIMPLE),
    _candidate("mid", QualityTier.MEDIUM),
    _candidate("big", QualityTier.COMPLEX),
    _candidate("thinker", QualityTier.REASONING),
)


def _ctx(text: str, system: str | None = None, **kwargs) -> RoutingContext:
    defaults = dict(
        user_text=text,
        system_prompt=system,
        estimated_input_tokens=len(text) // 4,
        has_images=False,
        has_tools=False,
        wants_json_schema=False,
        requested_max_tokens=None,
    )
    defaults.update(kwargs)
    return RoutingContext(**defaults)


# ── Classification (table-driven, English + Italian) ─────────────────────────


@pytest.mark.parametrize(
    ("prompt", "expected_tier"),
    [
        # simple
        ("What is the capital of France?", QualityTier.SIMPLE),
        ("Ciao, grazie!", QualityTier.SIMPLE),
        ("Cos'è una mela?", QualityTier.SIMPLE),
        # code-only → MEDIUM (faithful to the upstream boundaries)
        (
            "Write a python function that parses this SQL query and returns the schema",
            QualityTier.MEDIUM,
        ),
        (
            "Implementa una funzione che gestisce l'errore e ottimizza l'algoritmo di ordinamento",
            QualityTier.MEDIUM,
        ),
        # code + technical terms → COMPLEX
        (
            "Design a scalable distributed architecture: implement the python api "
            "with authentication, encryption and low latency database queries",
            QualityTier.COMPLEX,
        ),
        (
            "Progetta un'architettura distribuita e scalabile: implementa la funzione "
            "con autenticazione, crittografia e bassa latenza verso il database",
            QualityTier.COMPLEX,
        ),
        # explicit reasoning (2+ markers) → REASONING override
        (
            "Think through this step by step and explain your reasoning about the trolley problem",
            QualityTier.REASONING,
        ),
        (
            "Ragiona passo dopo passo e valuta i pro e contro di queste due opzioni",
            QualityTier.REASONING,
        ),
    ],
)
def test_classification_table(prompt: str, expected_tier: QualityTier) -> None:
    tier, _score, _signals = ComplexityStrategy().classify(prompt)
    assert tier is expected_tier, f"{prompt!r} → {tier}"


def test_word_boundary_prevents_false_positives() -> None:
    strategy = ComplexityStrategy()
    # "error" must not match inside "terrorism"; "api" not inside "capitale".
    tier, _, signals = strategy.classify("A short essay about terrorism in the 20th century")
    assert not any("code" in s for s in signals)
    tier_it, _, signals_it = strategy.classify("La capitale d'Italia?")
    assert not any("code" in s for s in signals_it)


def test_system_prompt_influences_code_but_never_reasoning() -> None:
    strategy = ComplexityStrategy()
    # Reasoning markers in the SYSTEM prompt must not force REASONING tier.
    tier, _, _ = strategy.classify(
        "Hello, thanks!", system_prompt="Think through step by step, evaluate pros and cons"
    )
    assert tier is not QualityTier.REASONING


async def test_select_picks_candidate_for_tier_and_records_signals() -> None:
    decision = await ComplexityStrategy().select(_ctx("Ciao, grazie!"), CANDIDATES)
    assert decision.model_name == "cheap"
    assert decision.strategy == "complexity"
    assert decision.tier == "SIMPLE"
    assert decision.decision_ms >= 0
    assert decision.signals


async def test_select_uses_nearest_tier_when_exact_is_unserved() -> None:
    two = (CANDIDATES[0], CANDIDATES[3])  # SIMPLE + REASONING only
    decision = await ComplexityStrategy().select(
        _ctx("Write a python function that parses SQL and returns the schema"), two
    )
    # COMPLEX unserved → nearest below (SIMPLE, since MEDIUM missing too).
    assert decision.model_name == "cheap"
    assert any("unserved" in s for s in decision.signals)


async def test_select_honours_explicit_tier_override() -> None:
    strategy = ComplexityStrategy({"tiers": {"SIMPLE": "thinker"}})
    decision = await strategy.select(_ctx("Ciao, grazie!"), CANDIDATES)
    assert decision.model_name == "thinker"


async def test_select_with_no_candidates_raises_for_caller_fallback() -> None:
    with pytest.raises(ValueError):
        await ComplexityStrategy().select(_ctx("hello"), ())


# ── Capability filters ───────────────────────────────────────────────────────


def test_filters_drop_incapable_candidates() -> None:
    candidates = (
        _candidate("visionless", QualityTier.MEDIUM),
        _candidate("vision", QualityTier.MEDIUM, supports_vision=True),
        _candidate("tools", QualityTier.MEDIUM, supports_tools=True),
        _candidate("tiny-window", QualityTier.MEDIUM, context_window_tokens=10),
    )
    assert filter_candidates(_ctx("x", has_images=True), candidates) == (candidates[1],)
    assert filter_candidates(_ctx("x", has_tools=True), candidates) == (candidates[2],)
    long_ctx = _ctx("x", estimated_input_tokens=50)
    assert candidates[3] not in filter_candidates(long_ctx, candidates)


def test_filter_json_schema() -> None:
    candidates = (
        _candidate("plain", QualityTier.MEDIUM),
        _candidate("structured", QualityTier.MEDIUM, supports_json_schema=True),
    )
    assert filter_candidates(_ctx("x", wants_json_schema=True), candidates) == (candidates[1],)


def test_nearest_tier_walks_down_then_up() -> None:
    only_reasoning = (_candidate("thinker", QualityTier.REASONING),)
    found = nearest_tier_candidate(QualityTier.SIMPLE, only_reasoning)
    assert found is not None and found.model_name == "thinker"
    assert nearest_tier_candidate(QualityTier.SIMPLE, ()) is None


# ── Context extraction (shared helper) ───────────────────────────────────────


def test_context_from_chat_messages_with_multimodal_blocks() -> None:
    request = {
        "messages": [
            {"role": "system", "content": "You are helpful"},
            {"role": "assistant", "content": "hi"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            },
        ],
        "tools": [{"type": "function"}],
        "response_format": {"type": "json_schema", "json_schema": {}},
        "max_tokens": 42,
    }
    ctx = build_routing_context(request)
    assert ctx.user_text == "describe this"
    assert ctx.system_prompt == "You are helpful"
    assert ctx.has_images and ctx.has_tools and ctx.wants_json_schema
    assert ctx.requested_max_tokens == 42


def test_context_from_responses_input() -> None:
    assert build_routing_context({"input": "quick question"}).user_text == "quick question"
    ctx = build_routing_context(
        {
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "from items"}]}],
            "text": {"format": {"type": "json_schema"}},
        }
    )
    assert ctx.user_text == "from items"
    assert ctx.wants_json_schema


def test_context_last_user_message_wins() -> None:
    request = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "…"},
            {"role": "user", "content": "second"},
        ]
    }
    assert build_routing_context(request).user_text == "second"
