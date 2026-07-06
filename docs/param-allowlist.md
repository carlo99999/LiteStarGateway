# Design doc — Request parameter allowlist

> **Status:** Implemented. `domain/request_policy.py` provides
> `sanitize_request(operation, request)` (deny-by-default allowlist per
> operation, clamps `n`→`MAX_N` = 8 and token fields →`MAX_TOKENS` = 32000) plus
> `clamp_output_tokens(operation, request, ceiling)` (per-model output ceiling),
> both wired into `CompletionService`. Cross-provider structured-output
> translation is also in (`infrastructure/llm/structured_output.py`, see §3.1).
> The rest of this doc is the original design rationale.

## 1. Goal

Stop forwarding arbitrary client fields to the provider SDKs. Today the adapters
build `{**model.params, **request}` with only `model` overridden, so any other
client field (`extra_headers`, `extra_body`, a huge `n`, transport overrides, …)
passes through. We want an **allowlist per operation**: deny-by-default, with
bounded numeric ranges.

## 2. Where it hooks

`CompletionService` is the single choke point. Sanitize the OpenAI-shaped
`request` **before** it reaches the gateway — ideally inside `_prepare` (or a
dedicated step it calls), so every op (`chat_completion`, `responses`,
`embeddings`, `images`, and their streaming variants) is covered uniformly.

```text
domain/request_policy.py     pure: sanitize_request(operation, request) -> dict   (+ allowlists/caps)
                             pure: clamp_output_tokens(operation, request, ceiling) -> dict
application/completion_service.py   calls sanitize_request() before dispatching
```

Keep it a **pure function** in the domain (no I/O) → trivially unit-testable.

## 3. Design

- **Allowlist per operation** (deny by default). Indicative sets:
  - chat.completions: `messages, model, temperature, top_p, max_tokens, max_completion_tokens,
    stop, n, presence_penalty, frequency_penalty, logit_bias, response_format,
    tools, tool_choice, seed, stream, stream_options, user`.
  - responses: `input, instructions, model, max_output_tokens, temperature, top_p,
    tools, tool_choice, text, reasoning, metadata, stream`.
  - embeddings: `input, model, dimensions, encoding_format, user`.
  - images: `prompt, model, size, quality, style, n, response_format, user`.
- **Numeric caps** (configurable): cap `n`, `max_tokens`/`max_completion_tokens`
  to a server maximum (global `MAX_TOKENS`, plus an optional per-model ceiling —
  see below).
- **Always stripped/blocked**: transport overrides — `extra_headers`,
  `extra_query`, `extra_body`, `base_url`, `api_key`, `organization`. (These are
  how a caller could exfiltrate the credential or inject headers.)
- **Precedence** — a model's admin config governs the request in three ways, by
  field, because different params want different semantics:
  - `model.params` are **defaults the client may override** (generation knobs
    like `temperature`/`top_p`): merged *before* the client request.
  - `model.params_enforced` is **admin policy the client cannot override** (a
    forced `response_format`, a locked `tool_choice`): merged *after* the client
    request. Frequently empty.
  - The effective request is `{**params, **client_request, **params_enforced}`
    (see `Model.merge_params`).
  - Cost ceilings are **not** governed by this merge: `model.max_output_tokens`
    (when set) clamps the client's output-token field down with `min` semantics
    and is injected when the client omits one, so it is a real cap the client
    cannot exceed or bypass by omission. It is applied in the completion service
    *before* the budget reservation, so admission and the provider call agree.
    Keep token/`n` fields out of `params_enforced` — the ceiling owns them.
- `model` is always forced to the resolved `provider_model_id` (already the case).

### 3.1 Cross-provider structured output

`response_format` (chat) / `text.format` (responses) is an allowlisted field, but
its OpenAI shape is not portable. `infrastructure/llm/structured_output.py`
parses it once — `parse_response_format(request)` → `StructuredOutput{name,
schema}` (or `None`) — so every adapter shares one interpretation:

- **OpenAI / Azure**: passed through natively.
- **Gemini (Vertex)**: mapped to `response_schema`.
- **Anthropic**: mapped to a forced tool (a tool whose input schema is the
  requested schema, with `tool_choice` forcing it).
- **Responses API**: the `text.format` shape carries the same spec.

`{"type": "json_object"}` → schema-less JSON; `{"type": "json_schema", …}` →
schema-constrained.

## 4. Open decisions

1. **Strip silently vs reject (400)** an unknown/over-cap field. Reject is
   clearer and safer; strip is friendlier. Lean: reject unknown keys with a
   generic 400 listing the rejected keys; clamp numerics with a warning.
2. **Allowlist scope**: global vs per-team override (some teams may need extra
   fields). Start global; add per-team later if needed.
3. **Maintenance**: provider APIs evolve; the allowlist needs an owner / periodic
   review. Consider sourcing from a single versioned constant.
4. **Passthrough escape hatch**: an admin-only `allow_raw` flag on a `Model` for
   advanced cases? (deny by default, opt-in).

## 5. Testing

- Pure tests on `sanitize()`: unknown key rejected/stripped; `n`/`max_tokens`
  clamped; transport keys removed; `model.params` wins over client.
- Integration: a chat request with `extra_headers` is rejected/stripped and never
  reaches the (faked) SDK.

## 6. Rollout

1. `feat/param-allowlist` — pure `sanitize()` + caps + wiring in
   `CompletionService` + tests. Single, self-contained branch.
