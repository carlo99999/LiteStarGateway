# Plan 01 — Native provider endpoints

**Design doc:** [`docs/next-steps/native-provider-endpoints.md`](../docs/next-steps/native-provider-endpoints.md)
**Depends on:** R7-H23 (untranslatable tools/vision now rejected) — done.
**Theme:** let users point the **native** Anthropic and Gemini SDKs at the gateway
(`base_url=<gateway>`), not just the OpenAI-compatible surface. This is the product
differentiator: native tool-calling and provider-specific features work without the
lossy OpenAI translation layer.

**Status: ✅ COMPLETE.** Phase 1 (Anthropic `/v1/messages` — 1a non-streaming, 1b
streaming, 1c real-SDK validation + docs) and Phase 2 (Gemini `generateContent` /
`streamGenerateContent` — dispatch + real-SDK validation + docs) are shipped:
governed passthrough, native metering, provider guards, real-SDK validation,
documentation and native conformance contracts. Reserved SDK kwargs are rejected
and output-token ceilings are applied; other provider fields remain verbatim.
Auth accepts `x-api-key` (Anthropic) and `x-goog-api-key` (Gemini). Deferred:
smart routing on native endpoints (backlog), Anthropic cache-token billing.

## Guardrails (from the design doc — non-negotiable)

1. **Reuse every existing guard — do not fork the middleware.** Auth
   (`infrastructure/web/auth.py`, `principal.py`), per-IP/per-key rate limiting
   (`rate_limit.py`), model resolution and enable/type checks
   (`application/completion_service._prepare`), and the credential-side `base_url`
   (never client-supplied) all apply unchanged.
2. **Meter natively, reuse the money core.** No new billing path: go through
   `UsageMeter` (`admit → settle_ok → release`). Extract native usage by inverting
   the existing response mappers (`from_anthropic_response`, `from_gemini_response`).
3. **Streaming: relay raw, meter at the tail.** Pass the provider's native SSE
   through untouched; settle usage from the final message/`usageMetadata` after the
   stream closes, with the same disconnect-safe settlement the OpenAI stream uses.
4. **Routing scope: none in phase 1.** Native endpoints resolve a concrete model;
   smart routing (judge/embeddings/hybrid) is not offered here yet.

## Phase 0 — Shared plumbing (1 slice)

Stand up the surface without provider logic, so auth/rate-limit/metering wiring is
proven once and reused.

- New router module `infrastructure/web/native/` (mirror an existing resource dir,
  e.g. `models/`): router + controller + dependencies, registered in
  `api_router`.
- Wire the **same** auth guard, rate-limit middleware, and DI providers the
  OpenAI-compatible controllers use — by composition, not copy.
- A `_prepare`-style resolution entry that maps the native request's model field to
  a team `Model` and runs the enable/type/credential checks.
- **Tests:** unauthenticated → 401; over-rate-limit → 429; unknown/disabled model →
  the correct domain status. No provider call yet (stub the dispatch).
- **Done when:** the endpoint exists, is fully guarded, and bills nothing because it
  does nothing.

## Phase 1 — Anthropic `POST /v1/messages`

> Detailed, implementation-ready breakdown (design decisions resolved, task list,
> TDD tests, file touchpoints): [`01a-anthropic-messages-phase1.md`](01a-anthropic-messages-phase1.md).

### 1a. Non-streaming

- Accept the native Anthropic Messages request shape; resolve the model; fetch
  credentials (server-side `base_url`).
- Dispatch to `infrastructure/llm/anthropic_adapter.py` in **native passthrough**
  mode (after reserved-kwarg rejection and output-token clamping, send the body
  and return the native response) — distinct from the OpenAI-translating path.
- **Metering:** derive `(input_tokens, output_tokens)` from the native
  `usage` block (invert `from_anthropic_response`) and run
  `admit → settle_ok → release` around the call, labelled e.g.
  `native.messages`.
- **Tests:** happy path returns the native body verbatim; a priced model records a
  usage event with the native token counts; over-budget team is rejected at
  `admit`; upstream provider error maps to a real HTTP status (not opaque 500).

### 1b. Streaming (`stream: true`)

- Relay the provider's raw SSE unchanged; do **not** re-encode through the OpenAI
  chunk shape.
- Settle usage at the tail from the final `message_delta`/`message_stop` usage,
  reusing the disconnect-safe settlement pattern from `completion_service`
  (`_prime`/`_rechain` + shielded settlement).
- **Tests:** start-of-stream provider error surfaces as an HTTP status before the
  200 commits (the H24 guarantee); a normal stream bills the tail usage exactly
  once; a client disconnect still settles.

### 1c. Validate with the real SDK

- E2E test (and a docs snippet) pointing the official `anthropic` SDK at the gateway
  with `base_url` + a gateway key, exercising a tool call end-to-end.
- **Done when:** `Anthropic(base_url=<gateway>, api_key=<gateway-key>)` completes a
  tool-calling turn, billed correctly, with no OpenAI translation in the path.

## Phase 2 — Gemini `POST /gemini/v1beta/models/{model}:generateContent`

Same slicing as phase 1, against `infrastructure/llm/vertex_adapter.py`:

- Native `generateContent` (non-streaming) + `streamGenerateContent` (SSE).
- Usage from `usageMetadata` (`promptTokenCount`/`candidatesTokenCount`), inverting
  `from_gemini_response`.
- Validate with the `google-genai` SDK (`HttpOptions(base_url=<gateway>)`).

## File touchpoints

- New: `infrastructure/web/native/` (router, controller, deps).
- Reused unchanged: `web/auth.py`, `web/principal.py`, `web/rate_limit.py`,
  `web/dependencies.py`, `web/exception_handlers.py`, `application/usage_meter.py`.
- Extended (native passthrough mode): `infrastructure/llm/anthropic_adapter.py`,
  `infrastructure/llm/vertex_adapter.py`; native-usage extraction alongside the
  existing `from_*_response` mappers.
- Registration: `infrastructure/web/api_router`.

## Risks & mitigations

- **Metering divergence** (native usage vs OpenAI-mapped) → assert in tests that the
  native path records the *same* token counts the OpenAI path would for an
  equivalent call.
- **Streaming settlement leaks on disconnect** → reuse the shielded-settlement
  pattern already proven by the OpenAI stream tests; add a disconnect test per
  provider.
- **Accidental middleware fork** → the phase-0 slice exists specifically so the
  guards are wired once; review that phase 1/2 add no parallel auth/limit logic.

## Execution

Sequential within a phase (streaming builds on non-streaming). Phase 1 and phase 2
are independent once phase 0 lands → phase 2 can run in a parallel worktree after
phase 1a defines the native-passthrough dispatch shape.
