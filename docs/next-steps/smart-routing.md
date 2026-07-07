# Implementation prompt: Smart Routing

> **Status — phase 1 implemented**: core contract (`domain/routing.py`:
> `RoutingStrategy` port, `RoutingContext`/`CandidateModel`/`RoutingDecision`),
> hard capability filters, S1 rule-based complexity strategy
> (`application/routing/complexity.py`, EN+IT keywords, ported from LiteLLM,
> MIT), decision persistence (`routing_decision` table), router CRUD
> (`/teams/{id}/routers`, `models:manage`), §4 fallback policy, and the offline
> eval harness (`tests/test_routing_eval.py`). Integration point is
> `CompletionService._prepare` as prescribed.
> **Phase 2 implemented**: S2 external webhook strategy
> (`application/routing/webhook.py`, contract in `docs/routing-webhook.md`)
> and shadow mode (fire-and-forget, persisted with `is_shadow`, failures
> swallowed). Phases 3-6 (observability endpoints + savings, embeddings,
> LLM judge, hybrid, export) are not yet implemented.

You are implementing **smart routing** for this LLM gateway (Litestar, hexagonal architecture, `src/litestar_gateway/`). Smart routing lets an admin define a **virtual model** (a "router") backed by N candidate models; every incoming request to the virtual model is dispatched to the best candidate according to a configurable strategy.

Read `CLAUDE.md`, `CONTRIBUTING.md`, and `domain/ports.py` first and follow the existing conventions: Protocol-based ports in `domain/`, services in `application/`, adapters in `infrastructure/`, dependency injection everywhere, full typing, tests mirroring the source tree.

---

## Core design (non-negotiable)

### 1. One contract, many strategies

Define a domain port:

```python
class RoutingStrategy(Protocol):
    async def select(self, ctx: RoutingContext, candidates: Sequence[CandidateModel]) -> RoutingDecision: ...
```

- `RoutingContext`: extracted user text (last user message, multimodal blocks flattened to text), system prompt, estimated input tokens, declared request features (has_images, has_tools, wants_json_schema, requested max_tokens), tenant/api-key identity.
- `CandidateModel`: model name + its **profile** (see §2).
- `RoutingDecision`: chosen model name, strategy id, tier/score if applicable, triggered signals (list of strings), decision latency in ms. Immutable (frozen dataclass).

The text-extraction helper (last user message + last system prompt, handling both string content and multimodal block lists) is written **once** and shared by all strategies.

### 2. Model profiles

Each candidate declares metadata the strategies consume:

- `description`: one line, human-written (fed to the LLM judge prompt)
- `quality_tier`: SIMPLE | MEDIUM | COMPLEX | REASONING (which tier this model serves)
- `capabilities`: vision, tools/function-calling, json_schema, max context window
- `input_cost_per_token`, `output_cost_per_token` (used for savings estimation, §7)

Store profiles in the router's config, not hardcoded. Router config itself is an entity: name, candidates with profiles, active strategy + per-strategy config, `default_model` (mandatory), optional shadow strategy (§6).

### 3. Hard capability filters — always, before any strategy

Before invoking the strategy, deterministically filter candidates:

- request contains images → keep only vision-capable candidates
- request contains `tools` → keep only function-calling candidates
- `response_format` json_schema → keep only candidates supporting it
- estimated input tokens > candidate context window → drop candidate

The strategy chooses among survivors only. If zero survive, fail the request with a clear domain error (this is a config problem, not a routing problem). If exactly one survives, skip the strategy entirely.

### 4. Failure policy

A routing failure must **never** fail the user request. Any strategy exception, timeout, or invalid output → log it, fall back to `default_model`. Give every strategy call a hard time budget (configurable, default 2000 ms for network-based strategies; the rule-based one needs none).

---

## Strategies (implement in this order)

### S1 — Rule-based complexity (port from LiteLLM)

Port `litellm/router_strategy/complexity_router/` (`complexity_router.py` + `config.py`, MIT licensed — keep attribution; upstream: <https://github.com/BerriAI/litellm>, itself inspired by ClawRouter). Weighted scoring over 7 dimensions (token count, code keywords, reasoning markers, technical terms, simple indicators with negative weight, multi-step regex patterns, question count), word-boundary keyword matching, reasoning-override at 2+ reasoning markers, configurable tier boundaries mapping score → SIMPLE/MEDIUM/COMPLEX/REASONING → candidate model.

Adaptations required:

- rewrite to this repo's conventions (frozen dataclasses, no mutation, typed, DI) — do not copy code style, copy the algorithm
- default keyword lists are English-only: **add Italian equivalents** for all four lists (code, reasoning, technical, simple) as part of the defaults
- tier → model mapping comes from candidate profiles' `quality_tier`, with explicit per-tier override in strategy config

### S2 — External webhook

Admin configures a URL (+ optional bearer token header). Contract:

```text
POST <url>              timeout: configurable, default 2s
{ "task": "<user text>", "system_prompt": "<or null>",
  "models": ["m1", "m2", ...], "metadata": { "estimated_tokens": 123 } }

→ 200 { "choice": 2 }   1-based index into "models",
                         or { "choice": "m2" } by name
```

Validate strictly at the boundary (Pydantic); out-of-range, non-2xx, timeout, malformed → fallback per §4. Document the contract in `docs/` and in the OpenAPI description so users can implement their own endpoint (could be a randint, could be their own ML model — the gateway does not care).

### S3 — Embeddings (semantic routes)

Admin defines routes: name → target model, example utterances, similarity threshold. At request time: embed the user text via a configured embedding model (through the gateway's own `LLMGateway` port — reuse existing adapters), cosine similarity against pre-computed utterance embeddings (computed lazily at first use, cached in memory), best route above threshold wins; below all thresholds → `default_model`. No external vector DB; in-memory is fine at this scale.

### S4 — LLM as a judge

A 5th model (the judge — admin should pick something small and fast) receives: the user text (truncated to a configurable char budget), and each candidate's name + `description` + `quality_tier`. It must answer via **constrained structured output** (tool call / json_schema with an enum over candidate names — never free text parsing; reuse `infrastructure/llm/structured_output.py` if applicable). Timeout + fallback per §4. The judge prompt is a versioned constant in code, not admin-editable in v1.

### S5 — Hybrid gray-zone (composition, not a new algorithm)

Wraps S1 + one escalation strategy (S4 or S2, configurable): run S1; if the weighted score falls within a configurable margin of a tier boundary (default ±0.08), the case is "uncertain" → invoke the escalation strategy for the final decision. Otherwise S1's answer stands. Record in `RoutingDecision.signals` whether escalation fired. This gives ~judge quality at ~10-20% of judge cost.

### S6 — Distillation data collection (not training)

Every routing decision made by S4 (and S5 escalations) is already persisted by §7. Add an admin export endpoint returning JSONL of `{text, system_prompt, chosen_model, strategy, score, signals, timestamp}` filtered by router/strategy/date range, so a classifier can be trained offline later. Training and serving a distilled classifier is **out of scope**; just make the data exportable and document the intended path (judge → dataset → small local classifier as a future strategy).

---

## Cross-cutting features

### 6. Shadow mode

A router may declare one **shadow strategy**: it runs after the active strategy decides (fire-and-forget task, never blocking or failing the request), and its would-be decision is persisted alongside the real one (`is_shadow` flag, same table). This enables validating S4 against live traffic before activating it, and A/B comparison of any two strategies on identical requests. Shadow runs respect the same time budget; shadow failures are logged and swallowed.

### 7. Decision observability

Persist every decision (new table via Alembic migration): router name, strategy, chosen model, tier/score, signals, decision latency ms, is_shadow, fallback_used, request id / api key id, timestamp. Then admin endpoints:

- decision list with filters (router, strategy, model, shadow, date range) + pagination (reuse the existing pagination envelope)
- aggregate stats: request distribution per chosen model / tier over time
- **estimated savings**: for each request, (cost it would have had on the most expensive capable candidate) − (cost on chosen model), using the request's actual token usage joined from existing usage metering. Expose the total per router per period. This is the headline metric.

### 8. Semantic cache (optional — build last, only if trivial after S3)

Behind a per-router flag, **off by default**. Before routing: embed user text (reuse S3 infrastructure), compare against recent cached (embedding, response) entries for the same router + same api key scope; above a high threshold (default 0.97) return the cached response without calling any model. TTL-bound, in-memory, bounded size (LRU). Mark cached responses in the decision log (`cache_hit`). If this cannot be done cleanly reusing S3's embedding path, skip it and note why.

---

## Explicit non-goals (do not build)

- Adaptive bandit routing, cascade/escalation-by-retry, sticky sessions, distilled-classifier training/serving, per-tenant routing preference headers. These are documented future work only.

## Delivery

Work in phases, each independently shippable and tested: (1) core contract + capability filters + S1 + decision persistence; (2) S2 + shadow mode; (3) observability endpoints + savings; (4) S3; (5) S4 + S5 + S6 export; (6) S8 if trivial. Integration point: the model-resolution step in `application/completion_service.py` (`_prepare`/`_dispatch`) — locate where the requested model name is resolved and insert the pre-routing hook there, mirroring how the rest of the request pipeline stays untouched (the strategy only rewrites the model name).

For each phase: unit tests for the strategy logic (table-driven where natural, e.g. classification cases in S1), integration test through the completion endpoint with a fake strategy, and a regression test proving §4 (strategy blows up → request still succeeds on default_model). Add a small offline eval script for S1/S5 with ~30 labeled prompts (mixed Italian/English) asserting a minimum tier-accuracy, so keyword/weight tuning has a harness.

State your assumptions before starting, per `CLAUDE.md`. If the existing model-resolution flow makes the single-integration-point design awkward, stop and propose alternatives instead of forcing it.
