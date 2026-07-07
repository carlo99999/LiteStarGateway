"""S1 — rule-based complexity strategy.

Algorithm ported from LiteLLM's complexity router
(`litellm/router_strategy/complexity_router/`, MIT — https://github.com/BerriAI/litellm),
itself inspired by ClawRouter (https://github.com/BlockRunAI/ClawRouter).
Ported by algorithm, not by code: rewritten to this repo's conventions
(frozen dataclasses, no mutation, DI). Local and sub-millisecond — no network.

Weighted scoring over 7 dimensions (token count, code keywords, reasoning
markers, technical terms, simple indicators with negative weight, multi-step
regex patterns, question count), word-boundary keyword matching, a reasoning
override at 2+ reasoning markers, and configurable score boundaries mapping to
SIMPLE/MEDIUM/COMPLEX/REASONING. Default keyword lists cover English AND
Italian. Tier → model comes from the candidates' `quality_tier` profiles, with
an optional per-tier override in the strategy config (`{"tiers": {...}}`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RoutingContext,
    RoutingDecision,
    estimate_tokens,
    nearest_tier_candidate,
)

STRATEGY_ID = "complexity"

# English defaults from LiteLLM, plus Italian equivalents (both always active).
DEFAULT_CODE_KEYWORDS: tuple[str, ...] = (
    # fmt: off
    "function",
    "class",
    "def",
    "const",
    "let",
    "var",
    "import",
    "export",
    "return",
    "async",
    "await",
    "try",
    "catch",
    "exception",
    "error",
    "debug",
    "api",
    "endpoint",
    "request",
    "response",
    "database",
    "sql",
    "query",
    "schema",
    "algorithm",
    "implement",
    "refactor",
    "optimize",
    "python",
    "javascript",
    "typescript",
    "java",
    "rust",
    "golang",
    "react",
    "vue",
    "angular",
    "node",
    "docker",
    "kubernetes",
    "git",
    "commit",
    "merge",
    "branch",
    "pull request",
    # Italian
    "funzione",
    "classe",
    "errore",
    "eccezione",
    "implementa",
    "implementare",
    "ottimizza",
    "ottimizzare",
    "algoritmo",
    "codice",
    "variabile",
    "compila",
    "richiesta",
    "risposta",
    "interroga",
    # fmt: on
)
DEFAULT_REASONING_KEYWORDS: tuple[str, ...] = (
    # fmt: off
    "step by step",
    "think through",
    "let's think",
    "reason through",
    "analyze this",
    "break down",
    "explain your reasoning",
    "show your work",
    "chain of thought",
    "think carefully",
    "consider all",
    "evaluate",
    "pros and cons",
    "compare and contrast",
    "weigh the options",
    "logical",
    "deduce",
    "infer",
    "conclude",
    # Italian
    "passo dopo passo",
    "passo passo",
    "ragiona",
    "ragioniamo",
    "analizza questo",
    "scomponi",
    "spiega il tuo ragionamento",
    "mostra i passaggi",
    "catena di pensiero",
    "pensa attentamente",
    "considera tutte",
    "valuta",
    "pro e contro",
    "confronta",
    "deduci",
    "inferisci",
    "concludi",
    "logico",
    # fmt: on
)
DEFAULT_TECHNICAL_KEYWORDS: tuple[str, ...] = (
    # fmt: off
    "architecture",
    "distributed",
    "scalable",
    "microservice",
    "machine learning",
    "neural network",
    "deep learning",
    "encryption",
    "authentication",
    "authorization",
    "performance",
    "latency",
    "throughput",
    "benchmark",
    "concurrency",
    "parallel",
    "threading",
    "memory",
    "cpu",
    "gpu",
    "optimization",
    "protocol",
    "tcp",
    "http",
    "grpc",
    "websocket",
    "container",
    "orchestration",
    # Italian
    "architettura",
    "distribuito",
    "scalabile",
    "microservizi",
    "apprendimento automatico",
    "rete neurale",
    "crittografia",
    "autenticazione",
    "autorizzazione",
    "prestazioni",
    "latenza",
    "concorrenza",
    "parallelo",
    "memoria",
    "ottimizzazione",
    "protocollo",
    # fmt: on
)
DEFAULT_SIMPLE_KEYWORDS: tuple[str, ...] = (
    # fmt: off
    "what is",
    "what's",
    "define",
    "definition of",
    "who is",
    "who was",
    "when did",
    "when was",
    "where is",
    "where was",
    "how many",
    "how much",
    "yes or no",
    "true or false",
    "simple",
    "brief",
    "short",
    "quick",
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank you",
    "goodbye",
    "bye",
    "okay",
    # Italian
    "cos'è",
    "che cos'è",
    "cosa è",
    "definisci",
    "definizione di",
    "chi è",
    "chi era",
    "quando è",
    "dov'è",
    "dove si trova",
    "quanti",
    "quante",
    "quanto costa",
    "sì o no",
    "vero o falso",
    "semplice",
    "breve",
    "corto",
    "veloce",
    "ciao",
    "salve",
    "grazie",
    "arrivederci",
    # fmt: on
)

DEFAULT_DIMENSION_WEIGHTS: dict[str, float] = {
    "tokenCount": 0.10,
    "codePresence": 0.30,
    "reasoningMarkers": 0.25,
    "technicalTerms": 0.25,
    "simpleIndicators": 0.05,
    "multiStepPatterns": 0.03,
    "questionComplexity": 0.02,
}
DEFAULT_TIER_BOUNDARIES: dict[str, float] = {
    "simple_medium": 0.15,
    "medium_complex": 0.35,
    "complex_reasoning": 0.60,
}
DEFAULT_TOKEN_THRESHOLDS: dict[str, int] = {"simple": 15, "complex": 400}

# Non-greedy .*? to prevent ReDoS on pathological inputs (upstream note).
_MULTI_STEP_PATTERNS = (
    re.compile(r"first.*?then", re.IGNORECASE),
    re.compile(r"prima.*?poi", re.IGNORECASE),
    re.compile(r"step\s*\d", re.IGNORECASE),
    re.compile(r"\d+\.\s"),
    re.compile(r"[a-z]\)\s", re.IGNORECASE),
)


@dataclass(frozen=True)
class _Dimension:
    name: str
    score: float
    signal: str | None = None


def _keyword_matches(text: str, keyword: str) -> bool:
    """Word-boundary matching for single words (so "error" never matches
    "terrorism"); substring matching for multi-word phrases."""
    lowered = keyword.lower()
    if " " not in lowered:
        return bool(re.search(r"\b" + re.escape(lowered) + r"\b", text))
    return lowered in text


def _score_keywords(
    text: str,
    keywords: tuple[str, ...],
    name: str,
    label: str,
    thresholds: tuple[int, int],
    scores: tuple[float, float, float],
) -> tuple[_Dimension, int]:
    low, high = thresholds
    score_none, score_low, score_high = scores
    matches = [kw for kw in keywords if _keyword_matches(text, kw)]
    if len(matches) >= high:
        return _Dimension(name, score_high, f"{label} ({', '.join(matches[:3])})"), len(matches)
    if len(matches) >= low:
        return _Dimension(name, score_low, f"{label} ({', '.join(matches[:3])})"), len(matches)
    return _Dimension(name, score_none), len(matches)


class ComplexityStrategy:
    """Classify by weighted signals, then pick the candidate serving the tier."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._weights = {**DEFAULT_DIMENSION_WEIGHTS, **config.get("dimension_weights", {})}
        self._boundaries = {**DEFAULT_TIER_BOUNDARIES, **config.get("tier_boundaries", {})}
        self._token_thresholds = {**DEFAULT_TOKEN_THRESHOLDS, **config.get("token_thresholds", {})}
        self._code = tuple(config.get("code_keywords") or DEFAULT_CODE_KEYWORDS)
        self._reasoning = tuple(config.get("reasoning_keywords") or DEFAULT_REASONING_KEYWORDS)
        self._technical = tuple(config.get("technical_keywords") or DEFAULT_TECHNICAL_KEYWORDS)
        self._simple = tuple(config.get("simple_keywords") or DEFAULT_SIMPLE_KEYWORDS)
        # Explicit tier → model overrides; otherwise candidate profiles decide.
        self._tier_overrides: dict[str, str] = dict(config.get("tiers", {}))

    def classify(
        self, prompt: str, system_prompt: str | None = None
    ) -> tuple[QualityTier, float, tuple[str, ...]]:
        # System prompt feeds code/technical/simple scoring (deployment-level
        # context); reasoning markers use the user text only, so a system
        # prompt can never force the REASONING tier.
        full_text = f"{system_prompt or ''} {prompt}".lower()
        user_text = prompt.lower()
        tokens = estimate_tokens(prompt)

        if tokens < self._token_thresholds["simple"]:
            token_dim = _Dimension("tokenCount", -1.0, f"short ({tokens} tokens)")
        elif tokens > self._token_thresholds["complex"]:
            token_dim = _Dimension("tokenCount", 1.0, f"long ({tokens} tokens)")
        else:
            token_dim = _Dimension("tokenCount", 0)

        code, _ = _score_keywords(
            full_text, self._code, "codePresence", "code", (1, 2), (0, 0.5, 1.0)
        )
        reasoning, reasoning_count = _score_keywords(
            user_text, self._reasoning, "reasoningMarkers", "reasoning", (1, 2), (0, 0.7, 1.0)
        )
        technical, _ = _score_keywords(
            full_text, self._technical, "technicalTerms", "technical", (2, 4), (0, 0.5, 1.0)
        )
        simple, _ = _score_keywords(
            full_text, self._simple, "simpleIndicators", "simple", (1, 2), (0, -1.0, -1.0)
        )
        multi_step = (
            _Dimension("multiStepPatterns", 0.5, "multi-step")
            if any(p.search(full_text) for p in _MULTI_STEP_PATTERNS)
            else _Dimension("multiStepPatterns", 0)
        )
        questions = (
            _Dimension("questionComplexity", 0.5, f"{prompt.count('?')} questions")
            if prompt.count("?") > 3
            else _Dimension("questionComplexity", 0)
        )

        dimensions = (token_dim, code, reasoning, technical, simple, multi_step, questions)
        signals = tuple(d.signal for d in dimensions if d.signal is not None)
        score = sum(d.score * self._weights.get(d.name, 0) for d in dimensions)

        if reasoning_count >= 2:  # explicit-reasoning override
            return QualityTier.REASONING, score, signals
        if score < self._boundaries["simple_medium"]:
            return QualityTier.SIMPLE, score, signals
        if score < self._boundaries["medium_complex"]:
            return QualityTier.MEDIUM, score, signals
        if score < self._boundaries["complex_reasoning"]:
            return QualityTier.COMPLEX, score, signals
        return QualityTier.REASONING, score, signals

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        start = perf_counter()
        tier, score, signals = self.classify(ctx.user_text, ctx.system_prompt)

        override = self._tier_overrides.get(tier.value)
        chosen: str | None = None
        if override and any(c.model_name == override for c in candidates):
            chosen = override
        if chosen is None:
            candidate = nearest_tier_candidate(tier, candidates)
            if candidate is not None:
                chosen = candidate.model_name
                if candidate.quality_tier is not tier:
                    signals = (*signals, f"tier {tier} unserved; nearest {candidate.quality_tier}")
        if chosen is None:  # no candidates at all — caller falls back (§4)
            raise ValueError("complexity strategy received no candidates")

        return RoutingDecision(
            model_name=chosen,
            strategy=STRATEGY_ID,
            tier=tier.value,
            score=score,
            signals=signals,
            decision_ms=(perf_counter() - start) * 1000,
        )
