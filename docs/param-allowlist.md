# Design doc — Request parameter allowlist

> **Status:** Draft / parked for future development. Lives on branch
> `adding-param-allowlist`. No code yet.

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

```
domain/request_policy.py     pure: sanitize(operation, request) -> dict   (+ allowlists/caps)
application/completion_service.py   calls sanitize() before dispatching
```

Keep it a **pure function** in the domain (no I/O) → trivially unit-testable.

## 3. Design

- **Allowlist per operation** (deny by default). Indicative sets:
  - chat: `messages, model, temperature, top_p, max_tokens, max_completion_tokens,
    stop, n, presence_penalty, frequency_penalty, logit_bias, response_format,
    tools, tool_choice, seed, stream, stream_options, user`.
  - responses: `input, instructions, model, max_output_tokens, temperature, top_p,
    tools, tool_choice, text, reasoning, metadata, stream`.
  - embeddings: `input, model, dimensions, encoding_format, user`.
  - images: `prompt, model, size, quality, style, n, response_format, user`.
- **Numeric caps** (configurable): cap `n`, `max_tokens`/`max_completion_tokens`
  to a server maximum.
- **Always stripped/blocked**: transport overrides — `extra_headers`,
  `extra_query`, `extra_body`, `base_url`, `api_key`, `organization`. (These are
  how a caller could exfiltrate the credential or inject headers.)
- **Precedence**: `model.params` (trusted admin config) is applied **after** the
  sanitized client request, so admin settings can't be overridden by clients.
- `model` is always forced to the resolved `provider_model_id` (already the case).

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
