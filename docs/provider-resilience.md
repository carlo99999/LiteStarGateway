# Design doc — Provider resilience (timeouts, retries, circuit breaker)

> **Status:** Draft / parked (pre-v1). Branch `adding-provider-resilience`.
> No code yet.

## 1. Goal

A gateway must not hang or amplify failures when an upstream provider is slow or
down. Today we call the provider SDKs with default (often unbounded) timeouts and
no retry/backoff policy. Add **timeouts**, **bounded retries with backoff**, and a
**circuit breaker** per provider/credential.

## 2. Design

- **Timeouts** (per operation): pass an explicit request timeout to each SDK
  (openai/anthropic/google-genai/boto3 all accept one). Configurable default +
  per-`Model` override. A hung upstream fails fast with `504`-style mapping.
- **Retries**: retry only **idempotent, transient** failures (timeouts, 429, 5xx)
  with exponential backoff + jitter, capped attempts. Never retry 4xx (bad
  request) or partially streamed responses. Respect `Retry-After` on 429.
- **Circuit breaker**: per `(provider, credential)` key, open the breaker after N
  consecutive failures for a cooldown window → fail fast (`503`) instead of
  hammering a dead upstream. Half-open probe to recover.
- **Placement**: a thin **decorator/wrapper around the adapter calls** in the
  gateway layer (or a `ResilientGateway` wrapping `LLMGatewayImpl`), so the policy
  is one place and adapters stay simple. Keep the breaker state in a store
  (in-memory per process; shared store for multi-worker — see deployment doc).

## 3. Streaming caveat

Retries apply only to the **connection/first-token** phase. Once bytes have been
streamed to the client, a mid-stream failure cannot be transparently retried —
surface it as a stream error. Document this.

## 4. Open decisions

1. **Library vs hand-rolled**: a small dependency (e.g. a circuit-breaker/retry
   lib) vs a minimal in-house policy. Lean: minimal in-house (few states), no new
   dep, fully testable.
2. **Breaker granularity**: per provider, per credential, or per model.
   Recommended: per `(provider, credential)`.
3. **Config surface**: global defaults in `Settings` + per-`Model` overrides.
4. **Interaction with fallback chains** (a possible later feature): breaker-open
   could trigger a fallback model.

## 5. Testing

- Pure tests of the retry policy (which errors retry, backoff sequence, cap) and
  the breaker state machine (closed→open→half-open→closed) with injected clock.
- Adapter tests with a faked SDK raising timeouts/429/5xx → assert retry/fail-fast
  behavior; no real network.

## 6. Rollout

1. `feat/provider-timeouts` — explicit timeouts + error→status mapping.
2. `feat/provider-retries` — retry policy (pure) + wiring.
3. `feat/circuit-breaker` — breaker wrapper + store + tests.
