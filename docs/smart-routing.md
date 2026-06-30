# Design doc — Smart routing (judge-based, by task difficulty)

> **Status:** Draft / parked for future development. Lives on branch
> `adding-smart-routing`. No code yet.

## 1. Goal

A single alias backed by **four models for four difficulty tiers** — `easy`,
`medium`, `hard`, `very_hard` — plus a **judge** that classifies each incoming
request and routes it to the matching model. The judge must be a **swappable
adapter** so we can change the classification strategy without touching the core.

Value: send easy prompts to a cheap/fast model and hard ones to a strong model —
cost/latency optimization with one endpoint.

## 2. Concept

Team-scoped entity **`SmartRoute`**:

- `id, team_id, name, enabled`
- `mapping: {easy, medium, hard, very_hard} -> model_id` (same team, same
  `ModelType`)
- `judge: <judge config>` (which judge adapter + its settings)
- `default_tier` for fallback when the judge fails.

## 3. The judge is a port (adapter pattern)

```
domain/ports.py        Judge (Protocol): classify(request) -> Difficulty
domain/entities.py     Difficulty (StrEnum: easy|medium|hard|very_hard), SmartRoute
infrastructure/routing/judges/
    llm_judge.py        asks a cheap model to classify (uses a Model+credential)
    heuristic_judge.py  length/keyword heuristics, zero extra cost/latency
    (future) external_judge.py  calls an external classifier service
```

Swapping the judge = swapping the adapter; the routing flow is unchanged. The
judge implementation is chosen per `SmartRoute` via its `judge` config.

## 4. Where it hooks

Like weighted routing, this sits **in front of model resolution** in
`CompletionService`:

1. Resolve alias → it's a `SmartRoute`.
2. `difficulty = await judge.classify(request)` (fallback to `default_tier` on
   error/timeout).
3. Resolve `mapping[difficulty]` → existing completion flow.

## 5. Tradeoffs / open decisions

1. **Latency**: an LLM judge adds a round-trip on the critical path (we need the
   decision before calling). Mitigate with a **small/fast judge model** or the
   **heuristic judge**. Document the cost.
2. **Judge cost**: offset by routing easy tasks to cheap models; still, the judge
   call costs tokens — surface it in usage/observability.
3. **Robust output**: force a strict enum (schema/tool-call) from the LLM judge;
   on any parse failure → `default_tier`.
4. **Privacy**: the LLM judge sees the prompt. If payloads are sensitive, prefer
   the heuristic judge (no external exposure).
5. **Caching**: optionally cache classification for identical prompts.
6. **Streaming**: judge first (non-streamed), then stream from the chosen model.
7. **Shared `Router` port** with weighted routing (`weighted-routing.md`): both
   are "decide a model, then delegate". A common abstraction (weighted + judge
   implementations) keeps `CompletionService` clean.

## 6. Testing

- `heuristic_judge` pure tests (inputs → tier).
- `llm_judge` with a faked classifier model; bad output → `default_tier`.
- Routing: each tier maps to the expected (faked) model; judge failure uses the
  default; deterministic via an injected/faked judge.

## 7. Rollout

1. `feat/smart-route-entity` — `SmartRoute` + `Difficulty` + repo + CRUD +
   validation.
2. `feat/judge-port` — `Judge` port + `heuristic_judge` (no latency) wired into
   `CompletionService`.
3. `feat/llm-judge` — LLM-backed judge adapter with strict output + fallback.
