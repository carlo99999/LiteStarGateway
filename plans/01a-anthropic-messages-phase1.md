# Plan 01a — Native Anthropic `/v1/messages`, Phase 1 (detailed)

Detailed, implementation-ready breakdown of **Phase 1** of
[Plan 01](01-native-provider-endpoints.md). Planning only — no code yet.

## What Phase 0 already gave us (merged)

- `POST /v1/messages` exists on the shared protected `api_router`, so it inherits
  the **same** `APIKeyAuthMiddleware` instance and the **same**
  `build_inference_rate_limit()` per-IP limiter as `/v1/chat/completions` — no fork.
- `CompletionService.prepare_native(team_id, expected_type, request)` resolves the
  model alias and runs the shared `_ensure_usable` guards (exists / enabled / type)
  - credential fetch. No routing, no `admit` yet.
- The controller currently raises `UnsupportedOperation` → 501 after the guards.

Phase 1 replaces that 501 with a real, metered native dispatch.

## Design decisions (resolved during planning)

1. **Native passthrough bypasses the translators.** The client already sends the
   native Anthropic Messages body and expects the native response, so we do NOT
   call `to_anthropic_request` / `from_anthropic_response`. We send `data` (the raw
   body) to `client.messages.create(**data)` and return `message.model_dump()`
   verbatim.
2. **Metering reuses the money core with zero new billing path.** Anthropic's native
   `usage` block is `{"input_tokens", "output_tokens", ...}`. `UsageMeter._parse_usage`
   **already** reads `input_tokens`/`output_tokens` (it handles the Responses API).
   So we pass `{"usage": <native usage block>}` straight into `settle_ok` — no
   translation. `admit` works on the native body too: it reads `max_tokens` (which
   the Anthropic API requires) via `_max_output_tokens`, choices = 1 (Anthropic
   returns one message; aligns with `Provider.honors_n == False`).
3. **Dispatch seam lives where the existing one does.** Add native methods to the
   `LLMGateway` port (`domain/ports`) and `LLMGatewayImpl` (`infrastructure/llm/gateway.py`)
   that resolve the adapter and call **new native methods** on `AnthropicAdapter`
   — crucially WITHOUT the `arun_translated` / `translate_stream` wrappers (those
   translate; native must not). Orchestration (admit/settle/release) lives on
   `CompletionService`, mirroring `chat_completion` / `open_chat_stream`.
4. **Provider guard: Anthropic-only.** `/v1/messages` is the Anthropic wire shape;
   if the resolved model's provider is not `ANTHROPIC`, reject with a clear domain
   error (→ 400/409) — a Gemini/OpenAI model behind this endpoint is a
   misconfiguration, not a translation opportunity.
5. **Credentials `base_url` stays server-side** (`_base_url(credentials)`), never
   from the client — unchanged from every other path.

## Slice 1a — non-streaming (`POST /v1/messages`)

### Tasks

- **Port:** add `anative_messages(request, model, credentials) -> dict` to the
  `LLMGateway` port (`domain/ports`). Signature only; no infra import.
- **Gateway impl:** `LLMGatewayImpl.anative_messages` — `_resolve(provider,
  "chat.completions")` (reuse the capability map), call the adapter's native method
  directly, wrapped in the same error-normalization the translated paths use
  (`arun_translated`-equivalent that does NOT translate the body/response). Confirm
  `errors.py` mapping still applies to native upstream errors.
- **Adapter:** `AnthropicAdapter.anative_messages(native_body, model, credentials)`
  — build client with `_base_url(credentials)`, `await client.messages.create(**native_body)`,
  return `message.model_dump()` (raw). No `to_/from_` translation.
- **Native usage extraction:** a tiny helper (in the adapter or a native-usage
  module) returning the native `usage` dict from the response, for settlement.
- **Application orchestration:** `CompletionService.native_messages(team_id,
  api_key_id, data)` — `prepare_native` (from Phase 0) → provider guard (Anthropic)
  → `admit(team_id, model, data)` → dispatch via gateway → `settle_ok(..., operation="native.messages", response={"usage": native_usage})` → `release`,
  wrapped exactly like `_dispatch` (trace on error, release in `finally`).
- **Controller:** replace the 501 stub — call `native_messages`, return the raw
  native body as JSON (`Response`), status 200. Keep the `data.get("stream")` branch
  raising 501 until 1b lands.

### Tests (`tests/native/test_messages.py`, extend)

- Happy path: native request → native response body returned **verbatim** (assert
  the Anthropic shape, not the OpenAI shape).
- Billing: a priced Anthropic model records **one** usage event with the native
  `input_tokens`/`output_tokens`; assert the counts match the fake provider's usage.
- Over-budget: a team past its cap is rejected at `admit` (never dispatches).
- Provider guard: a non-Anthropic model behind `/v1/messages` → the chosen 4xx.
- Upstream error: fake provider raising maps to a real HTTP status (not opaque 500),
  and nothing is billed (reservation released).

### Done when

`Anthropic(base_url=<gateway>, api_key=<key>).messages.create(...)` returns a real
native response, billed once with native token counts, no OpenAI translation in the
path, full gate green.

## Slice 1b — streaming (`stream: true`)

### Tasks

- **Port + gateway:** `astream_native_messages(request, model, credentials) ->
  AsyncIterator[...]` — relays the provider's **raw** Anthropic SSE events; must NOT
  go through `translate_stream` (which re-encodes to OpenAI chunks).
- **Adapter:** `AnthropicAdapter.astream_native_messages` — `client.messages.create(**{**native_body, "stream": True})`,
  yield each raw event (`event.model_dump()`), close client in `finally`.
- **Native metered wrapper:** mirror `CompletionService._metered` + `_prime` /
  `_rechain`, but accumulate usage from the **raw Anthropic stream**:
  `message_start` carries `usage.input_tokens`; `message_delta` carries the running
  `usage.output_tokens`; settle at the tail from the last seen values. Reuse the
  release-exactly-once (`weakref.finalize`) + disconnect-safe (`_rechain`'s
  `finally: aclose()`) machinery so a client disconnect still settles (the H24 /
  shielded-settlement guarantees).
- **Controller:** on `data.get("stream")`, prime the stream (so an open-time error
  surfaces as HTTP status before the SSE 200 commits) and return it as SSE, mirroring
  how the `/v1/chat/completions` streaming response is constructed.

### Tests

- Start-of-stream provider error surfaces as an HTTP status before 200 (H24 parity).
- A normal stream relays raw Anthropic events unchanged and bills the tail usage
  **exactly once**.
- Client disconnect mid-stream still settles (assert a usage event / released
  reservation).

### Done when

The native `anthropic` SDK streams a tool-calling turn through the gateway, raw
events intact, billed once, disconnect-safe.

## Slice 1c — real-SDK validation + docs

- E2E test pointing the official `anthropic` SDK at an in-process gateway
  (`base_url` + gateway key), exercising a **tool-call round-trip** (request →
  `tool_use` → tool_result → final) against a fake upstream.
- Docs snippet in `docs/` (and cross-link from
  `docs/next-steps/native-provider-endpoints.md`): copy-paste native-SDK setup.
- **Done when:** a native tool-calling turn completes end-to-end, billed correctly,
  documented.

## File touchpoints

- `domain/ports` (LLMGateway port: two native method signatures).
- `infrastructure/llm/gateway.py` (native dispatch methods, no translate wrappers).
- `infrastructure/llm/anthropic_adapter.py` (native non-stream + stream methods,
  native-usage helper).
- `application/completion_service.py` (`native_messages`, `open_native_messages_stream`,
  a native metered wrapper — factor shared bits with `_dispatch` / `_metered`).
- `infrastructure/web/native/controller.py` (replace 501 stub; add stream branch).
- Tests under `tests/native/`.

## Risks & mitigations

- **Metering divergence native-vs-OpenAI** → a test asserting the native path bills
  the same token counts the translated path would for an equivalent call.
- **Accidental translation in the native path** → the native gateway methods must
  not call `arun_translated`/`translate_stream`; assert the returned body is the
  native Anthropic shape (has `content`/`stop_reason`, not `choices`).
- **Streaming settlement leak on disconnect** → reuse the proven
  `_metered`/`_rechain` machinery; add a per-provider disconnect test.
- **Cache-token billing** (Anthropic `cache_creation_input_tokens` /
  `cache_read_input_tokens`) → out of scope for Phase 1; count input+output only,
  note as a follow-up (billing rate for cache tokens is a pricing decision).

## Sequencing

1a → 1b → 1c, sequential (1b builds on the native dispatch shape defined in 1a;
1c validates both). Each slice is its own branch/PR with the full gate. When
Phase 1 lands, Plan 01 Phase 2 (Gemini) can start in a parallel worktree by
mirroring this structure against `vertex_adapter.py` + `usageMetadata`.
