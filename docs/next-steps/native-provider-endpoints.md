# Implementation prompt: Provider-native endpoints (Anthropic Messages, Gemini generateContent)

> **Status — implemented.** Phase 1 (Anthropic Messages) and Phase 2 (Gemini
> generateContent) are shipped, metered, documented and conformance-locked.
> The rationale below is retained as the design record. Motivated by R7-H23: the
> OpenAI-compatible surface could not originally express tool/function calling
> or multimodal input on Anthropic, Vertex or Bedrock, so such requests failed
> with `UnsupportedOperation` (501) instead of being silently stripped.
> Non-streaming Anthropic tools and capability-gated Bedrock Claude 3/Nova
> tools have since gained bounded, conformance-tested translations. Translated
> streaming tools, Vertex tools and multimodal input remain fail-closed. The
> native wire protocols remain
> first-class gateway endpoints and let clients use the provider's **native SDK**
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

Non-goal for the native endpoints: cross-protocol translation. An
Anthropic-shaped request is served only by an Anthropic-backed model; a
Gemini-shaped request only by a Gemini-backed model. The separate
OpenAI-compatible adapter now translates bounded non-streaming Anthropic and
capability-gated Bedrock tool subsets; that does not change these passthrough
endpoints.

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
- **Native-body governance**: `request_policy` is OpenAI-shaped and is not
  reused wholesale. The shipped native path rejects reserved SDK body kwargs
  (`extra_headers`/`extra_body`/`extra_query`/`timeout` and private keys), clamps
  the provider output-token field and otherwise preserves the body verbatim.
  It deliberately does not merge `model.params` or `model.params_enforced`;
  native callers own provider-shaped inference fields.
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

That later phase is now designed in `routing-evolution.md` / Plan 12: Anthropic
may route only to Anthropic and Gemini only to Gemini, with config-time and
dispatch-time validation. It never introduces cross-protocol translation.

`CandidateModel.supports_tools` means that both the model and the selected
surface can honor the requested tool contract. The OpenAI path now honors its
bounded non-streaming contract for OpenAI/Azure/Databricks/Anthropic and
validated Bedrock Claude 3/Nova IDs; native Anthropic remains the route for
unrestricted or streaming Claude tool use. Vertex translation stays fail-closed
until its thought-signature contract is implemented.

## Suggested slicing

1. **Anthropic `/v1/messages`, non-streaming** — new `infrastructure/web/native/`
   router + controller; reuse auth/rate-limit/model-resolution; a thin
   `AnthropicNativeAdapter.messages(request, model, credentials)` that forwards
   the native body and returns the native response; native usage extractor +
   `UsageMeter` wiring; native param sanitizer. Tests: tool call round-trips
   (request `tools` → response `tool_use`), tool-result turn accepted, usage
   billed to `usage_event`, over-budget → 402/`BudgetExceeded`, tenant isolation,
   reserved transport-control fields rejected.
2. **Anthropic `/v1/messages`, streaming** — raw SSE relay through
   `metered_stream` with native tail-usage extraction and R7-H24 first-frame
   priming.
3. **Gemini `generateContent` + `:streamGenerateContent`** — same pattern with
   the `google-genai` SDK.
4. **Docs**: maintain a capability matrix page. The current
   OpenAI-compatible surface supports text on all providers, structured output
   where the selected model contract can enforce it, and non-streaming tools on
   OpenAI/Azure/Databricks/Anthropic plus validated Bedrock Claude 3/Nova IDs.
   Bedrock `json_schema` remains fail-closed pending a native per-model
   capability matrix. For full-fidelity tools/multimodal on
   Anthropic/Gemini, use the native endpoints with the provider SDK; Bedrock has
   no gateway-native endpoint.

## Acceptance

- An `anthropic` SDK client pointed at the gateway completes a full tool-use loop
  (model requests a tool, client returns a tool result, model finishes) with the
  spend recorded in `usage_event` and gated by the team budget.
- No new auth/tenant/transport-override hole (mirror the OpenAI path's guard
  tests for the native router).
- The OpenAI-compatible surface fails loudly for unsupported provider/feature
  combinations. Its bounded non-streaming Anthropic tool subset is supported;
  the bounded Bedrock subset is supported; translated streaming tools, Vertex
  tools and unsupported Bedrock choice/model combinations remain 501.

## Usage

Phase 1 (Anthropic `/v1/messages`) is implemented. For a copy-paste guide to
pointing the native `anthropic` SDK at the gateway — client setup, streaming,
and a full tool-call round-trip — see
[Native Anthropic Messages endpoint](../native-anthropic.md).

Phase 2 (Gemini `generateContent` / `:streamGenerateContent`) is implemented.
For the equivalent copy-paste guide to pointing the native `google-genai` SDK at
the gateway — client setup, streaming, and a full function-calling round-trip —
see [Native Gemini generateContent endpoint](../native-gemini.md).
