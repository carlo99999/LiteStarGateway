# Implementation prompt: Provider-native endpoints (Anthropic Messages, Gemini generateContent)

> **Status — proposed (not started).** Motivated by R7-H23: the OpenAI-compatible
> surface cannot express tool/function calling or multimodal input on Anthropic,
> Vertex or Bedrock (those translators are text-in/text-out + structured output),
> so such requests now fail with `UnsupportedOperation` (501) instead of being
> silently stripped. Rather than emulate OpenAI tool-calling on top of each
> provider (lossy, high-maintenance — you maintain a mapping between *two*
> evolving APIs), expose each provider's **native** wire protocol as a
> first-class gateway endpoint and let clients use the provider's **native SDK**
> pointed at the gateway `base_url`. The workload stays inside the gateway, so
> budget, keys, usage metering, audit, routing and rate-limiting all still apply —
> and tools/caching/multimodal/thinking pass through natively with nothing to get
> wrong.

You are implementing **provider-native passthrough endpoints** for this LLM
gateway (Litestar, hexagonal architecture, `src/litestar_gateway/`). Read
`CLAUDE.md`, `CONTRIBUTING.md`, and `domain/ports/` first and follow the existing
conventions: Protocol-based ports in `domain/`, services in `application/`,
adapters in `infrastructure/`, dependency injection everywhere, full typing,
tests mirroring the source tree.

---

## Goal

A customer running an agent framework on Claude or Gemini (Pydantic AI's
`AnthropicModel`/`GeminiModel`, the Anthropic SDK, the `google-genai` SDK, …)
should point that SDK's `base_url` at the gateway and get **full native
behaviour** — tool calling, parallel tool calls, tool-result turns, multimodal
input, prompt caching, extended thinking — while the gateway keeps enforcing the
same governance it enforces on `/v1/chat/completions`.

Non-goal: cross-protocol translation. An Anthropic-shaped request is served only
by an Anthropic-backed model; a Gemini-shaped request only by a Gemini-backed
model. No OpenAI↔provider tool-calling emulation (that is what R7-H23 deliberately
declined to build).

## Endpoints (phase 1: Anthropic; phase 2: Gemini)

| Endpoint | Wire shape | Backing provider | Client SDK |
| --- | --- | --- | --- |
| `POST /v1/messages` | Anthropic Messages | `anthropic` | `Anthropic(base_url=…, api_key=<gateway-key>)` |
| `POST /v1/messages` (`stream:true`) | Anthropic SSE | `anthropic` | same |
| `POST /gemini/v1beta/models/{model}:generateContent` | Gemini `generateContent` | `vertex_ai` | `google-genai` `HttpOptions(base_url=…)` |
| `:streamGenerateContent` | Gemini SSE | `vertex_ai` | same |

`model` in the request body (Anthropic) or path (Gemini) is the **team model
alias**, resolved to a `Model` exactly like the OpenAI path — never the upstream
`provider_model_id` and never a raw provider endpoint.

## Non-negotiable design

### 1. Reuse every existing guard — do not fork the middleware

The new endpoints are new attack surface. The recurring theme of the review is
"new surfaces outran governance." Each endpoint MUST reuse, from day one:

- **Auth**: the API-key middleware (`infrastructure/web/auth.py`). `request.user`
  is the team id; `request.auth` is the API key. Require the same inference scope
  the OpenAI endpoints require (`allows_inference`). No new auth realm.
- **Rate limiting**: wire `build_inference_rate_limit` onto the new router, same
  as `/v1/*`.
- **Model resolution + tenant scoping**: `ModelRepository.get_by_name(team_id,
  alias)`; reject a model whose `provider` doesn't match the endpoint's protocol
  with `ModelTypeMismatch`/`UnsupportedOperation`. Credentials only via
  `CredentialRepository.get_values`; the upstream `base_url` comes only from the
  credential (`_base_url`), never from the client — preserve the exfiltration
  guard that already exists in the adapters.
- **Param sanitization**: `request_policy` is OpenAI-shaped and cannot be reused
  as-is. Add a native allowlist per protocol that, at minimum, strips SDK
  transport overrides (`extra_headers`/`extra_body`/`extra_query`/`timeout`/
  `api_key`/`base_url`) so a tenant can't manipulate how we call upstream with
  *our* credential. `model.params_enforced` must still be merged in as trusted
  admin policy the client cannot override.
- **Errors**: reuse `translate_upstream_error` / `errors.py` so upstream
  4xx/5xx map to the same gateway statuses; SCIM-style native error bodies are
  not required, but a 501 for an unsupported model/protocol combination must use
  the existing `UnsupportedOperation` handler.

### 2. Metering: bill natively, reuse the money core

`UsageMeter.admit`/`settle_ok`/`release` and the cost math are provider-agnostic
once you have `(prompt_tokens, completion_tokens)`. The only new work is
extracting those counts from the **native** response/stream instead of the
OpenAI shape:

- Anthropic: `usage.input_tokens` / `usage.output_tokens` (+ `cache_creation_input_tokens`
  / `cache_read_input_tokens` — decide whether cache tokens bill at input rate;
  see R7-L31/L15 money notes). The extraction already exists inverted in
  `anthropic_adapter.from_anthropic_response`.
- Gemini: `usageMetadata.promptTokenCount` / `candidatesTokenCount` — already
  inverted in `vertex_adapter.from_gemini_response`.

Admission (`admit`) needs a pessimistic reservation: reuse `_reservation_cost`
but feed it a native-shaped adapter that yields the same prompt-estimate +
output-ceiling numbers (map `max_tokens`/`maxOutputTokens`). Budget-gating and
`usage_event` billing then work unchanged, so native traffic shows up in
`/teams/{id}/usage` and counts against the budget window like any other call.

### 3. Streaming: relay raw, meter at the tail

Passthrough streaming is *simpler* than the current translate-to-OpenAI path:
relay the provider's native SSE frames unchanged. Wrap the native stream in the
existing `metered_stream` machinery (shielded settlement on disconnect, M27/M29
guarantees) but with a native usage extractor for the final/among frames
(Anthropic `message_delta.usage`; Gemini trailing `usageMetadata`). The
start-of-stream error-surfacing fix from R7-H24 applies here too — prime the
first frame before returning the SSE response so an upstream failure at stream
open becomes an HTTP status, not a 200 + aborted body.

### 4. Routing scope (phase 1: none)

Smart routing rewrites an OpenAI-shaped `model` alias. For phase 1 the native
endpoints do **not** support the smart router: the alias resolves directly to one
same-protocol model. Document that a router alias on a native endpoint is
rejected. (A later phase may allow "same-protocol-family" routing — a native
Anthropic request routing only over Anthropic candidates — but that needs the
routing context/strategies to be protocol-aware and is out of scope here.)

Note the capability mismatch this resolves: `CandidateModel.supports_tools`
currently means "the underlying model supports tools," but the OpenAI path can
only honour it for OpenAI/Azure. With native endpoints, an admin who wants tools
on Claude routes the client to the Anthropic model via `/v1/messages`, and
`supports_tools` on the OpenAI routing path keeps meaning "OpenAI/Azure tool
passthrough." Keep the R7-H23 501 on the OpenAI path and update its message to
point at the native endpoint.

## Suggested slicing

1. **Anthropic `/v1/messages`, non-streaming** — new `infrastructure/web/native/`
   router + controller; reuse auth/rate-limit/model-resolution; a thin
   `AnthropicNativeAdapter.messages(request, model, credentials)` that forwards
   the native body and returns the native response; native usage extractor +
   `UsageMeter` wiring; native param sanitizer. Tests: tool call round-trips
   (request `tools` → response `tool_use`), tool-result turn accepted, usage
   billed to `usage_event`, over-budget → 402/`BudgetExceeded`, tenant isolation,
   transport-override fields stripped.
2. **Anthropic `/v1/messages`, streaming** — raw SSE relay through
   `metered_stream` with native tail-usage extraction and R7-H24 first-frame
   priming.
3. **Gemini `generateContent` + `:streamGenerateContent`** — same pattern with
   the `google-genai` SDK.
4. **Docs**: a capability matrix page — "OpenAI-compatible surface: text +
   structured output on all providers; tools/multimodal on OpenAI/Azure only.
   For tools/multimodal on Anthropic/Gemini use the native endpoints with the
   provider SDK." Update the R7-H23 501 message to reference `/v1/messages`.

## Acceptance

- An `anthropic` SDK client pointed at the gateway completes a full tool-use loop
  (model requests a tool, client returns a tool result, model finishes) with the
  spend recorded in `usage_event` and gated by the team budget.
- No new auth/tenant/transport-override hole (mirror the OpenAI path's guard
  tests for the native router).
- The OpenAI-compatible surface is unchanged; the R7-H23 501 still fires for
  OpenAI-shaped tool requests to non-OpenAI models, now pointing at the native
  endpoint.
