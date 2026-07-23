# Plan 06 — Guardrails

**Design doc:** [docs/next-steps/guardrails.md](../docs/next-steps/guardrails.md).
**Depends on:** the request-sanitizing pipeline (`domain/request_policy.py`,
`application/completion_service.py`) and the audit trail (`domain/ports/audit.py`)
— both shipped. Console config (Phase 3) depends on Plan 03 (Admin UI) but the
backend guardrail surface does not.
**Theme:** an enterprise policy layer — pluggable content moderation + PII
detection/redaction on the request (pre-call) and response (post-call), able to
**block**, **redact**, or **flag**. Off by default, configured per team/model.

## Scope

A `GuardrailProvider` Protocol (`domain/guardrails.py`) with a `GUARDRAILS`
registry (mirroring `application/routing/service.py:81`), three built-in
providers, and two hook points in `CompletionService`:

- **pre-call** in `_prepare` (line 497), after `clamp_output_tokens` (line 566)
  and before `_meter.admit` (line 567);
- **post-call** in `_dispatch` (line 135) before `settle_ok`, plus the streaming
  path via `open_chat_stream` (line 614) / `_metered`.

Verdicts: `ALLOW | REDACT | BLOCK`. `BLOCK` raises a new
`GuardrailBlocked(DomainError)`. Every `BLOCK`/`REDACT` is audited with
categories/counts only — never raw matches.

## Phases

### Phase 0 — Port, verdict types, hook points

- `domain/guardrails.py`: `GuardrailProvider` Protocol, `Direction`,
  `GuardrailPayload`, `GuardrailVerdict` (all frozen), reusing the routing
  text-extraction helper. `GuardrailBlocked` added to `domain/exceptions.py`.
- `application/guardrails/service.py`: `GUARDRAILS` registry, per-direction chain
  runner (parallel independent checks via `asyncio.gather`, any-`BLOCK`-wins,
  `REDACT` composition, per-provider `fail` policy resolution).
- Wire both hook points into `CompletionService` with an **empty chain** default
  (no behavior change for existing tenants). Controller maps `GuardrailBlocked`
  to a 4xx error envelope.
- **Done when:** with no policy configured, the full existing suite is green and
  a fake pass-through provider proves the pre/post hooks fire at the documented
  positions (pre before `admit`, post before `settle_ok`).

### Phase 1 — Regex/rule PII provider

- `application/guardrails/pii_regex.py`: pure detectors + redaction for email,
  phone (E.164 + IT), credit card (Luhn), IBAN (mod-97), API keys/secrets,
  IT codice fiscale. Stable typed placeholders; verdict carries categories +
  counts only.
- **Done when:** table-driven unit tests cover true positives, checksum-gated
  false-positive rejection (invalid Luhn/IBAN/CF), and idempotent redaction; an
  integration test proves a `redact` pre-policy rewrites the body the fake
  gateway receives, and a `redact` post-policy rewrites the returned response.

### Phase 2 — LLM / native moderation provider

- `application/guardrails/moderation.py`: gateway-model backend (constrained
  json_schema via `infrastructure/llm/structured_output.py`, called through the
  `LLMGateway` port) and provider-native `/v1/moderations` backend, selected by
  config; hard time budget (default 2000 ms).
- **Done when:** a `block` pre-policy over a threshold returns `GuardrailBlocked`
  **before the completion gateway is called** and is audited; unit tests cover
  score→category mapping and threshold boundaries with a fake moderation gateway.

### Phase 3 — Webhook provider + console config

- `application/guardrails/webhook.py` mirroring `docs/routing-webhook.md`;
  contract documented in `docs/guardrails-webhook.md`; strict Pydantic boundary
  validation. Team/model policy CRUD + RBAC, `bearer_token` masked
  (`BEARER_TOKEN_MASK`). Admin UI: per-team/model chains + recent
  `guardrail.block`/`guardrail.redact` feed.
- **Done when:** an admin can configure a webhook guardrail end-to-end, malformed
  webhook responses honor the `fail` policy, and the UI never renders raw
  secrets or matched PII.

## TDD strategy

- **Unit (table-driven):** PII detection/redaction cases per category, including
  checksum edge cases and multilingual (IT) inputs; moderation score→verdict
  mapping; webhook response parsing (valid/malformed/out-of-range).
- **Integration:** a `BLOCK` verdict returns `GuardrailBlocked` **before** the
  provider is invoked (assert the fake `LLMGateway` was never called) and writes
  exactly one audit event; a `REDACT` verdict changes what the fake gateway
  receives (pre) and what the caller gets back (post).
- **Regression (fail-open/closed):** a guardrail provider that raises must honor
  its configured policy — a blocking provider `fail: "closed"` blocks the
  request, a redacting provider `fail: "open"` proceeds with a warning + audit —
  and **never silently leaks** an unscreened prompt/response. One test per
  direction pinning both defaults.
- **No-PII-in-logs:** assert audit `detail` and any log line contain only
  categories/counts, never a matched value.

## Risks & mitigations

- **Latency** → P1 is pure/sub-ms; P2/P3 network calls get a hard timeout and run
  in parallel within a chain; blocking providers short-circuit. Per-provider
  `latency_ms` recorded for observability.
- **False positives** (over-blocking, over-redacting) → checksum-gated detectors
  (Luhn/IBAN/CF), configurable moderation thresholds, `flag`-only action for
  tuning before enforcement, offline fixture corpus for regression.
- **PII in logs** → categories/counts only in audit/logs; redact before logging;
  a dedicated test enforces it (the `AuditEvent` "never store secrets" rule).
- **Streaming leakage** → default to block-streaming-when-a-blocking-post-
  guardrail-is-active; delay-window incremental scan is opt-in and bounds leakage
  to the window size; never unwind an already-emitted chunk.
- **Wrong-default availability risk** → blocking guardrails fail **closed**,
  redacting fail **open**, both explicit and configurable; regression-pinned.

## Execution

- One branch per phase, TDD (RED→GREEN), gate before every PR (`just test`,
  `just lint`, `just typecheck`, `just pre-commit`).
- Hexagonal boundary is law: port + verdict types in `domain/`, providers +
  chain runner in `application/`, webhook HTTP client + native `/v1/moderations`
  call in `infrastructure/`.
- Phases 0→1→2 are backend-only and sequential (each builds on the port); Phase 3
  webhook is independent of P1/P2 and its console slice can run in the Plan 03
  track. If team/model policy needs a schema column, ship an Alembic migration
  with Phase 3 (per `CONTRIBUTING.md`).
- State assumptions before starting, per `CLAUDE.md`. If the single pre/post
  choke point in `CompletionService` proves awkward for any operation, stop and
  propose alternatives rather than forcing it.
