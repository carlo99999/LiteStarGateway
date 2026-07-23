# Implementation prompt: Guardrails

> **Status — not started (design only).** No code exists yet. This mirrors the
> pluggable-strategy shape already proven by smart routing
> (`domain/routing.py`: `RoutingStrategy` port + `application/routing/service.py`
> `STRATEGIES` registry at line 81 + `default_model` fallback policy) and slots
> into the same request choke point as the param allowlist
> (`domain/request_policy.py`). Integration point is `CompletionService`
> (`application/completion_service.py`): the **pre-call** hook runs in `_prepare`
> (line 497) after `clamp_output_tokens` (line 566) and **before** `_meter.admit`
> (line 567); the **post-call** hook runs in `_dispatch` (line 135) on the
> response before `settle_ok`, and on the metered stream via `open_chat_stream`
> (line 614) / `_metered`. See §4 for exact placement and why.

You are implementing **guardrails** for this LLM gateway (Litestar, hexagonal,
`src/litestar_gateway/`). A guardrail is a pluggable policy that inspects a
request **before** it reaches the provider and/or a response **after** it comes
back, and returns a verdict: **allow**, **redact** (rewrite the payload and
proceed), or **block** (fail the request with a clear domain error). The
motivating use cases are content moderation and PII detection/redaction, applied
either pre-call (on the prompt) or post-call (on the completion).

Read `CLAUDE.md`, `CONTRIBUTING.md`, `domain/routing.py`, and
`docs/param-allowlist.md` first, and follow the existing conventions: Protocol
ports in `domain/`, services in `application/`, adapters in `infrastructure/`,
DI everywhere, full typing, frozen dataclasses, many small files, TDD.

Guardrails are **off by default** and configured per team/model — the default
posture of a gateway that already ships secure-by-default sanitizing must not
change for existing tenants.

---

## Core design (non-negotiable)

### 1. One contract, many providers

Define a domain port, mirroring `RoutingStrategy`:

```python
class GuardrailProvider(Protocol):
    async def check(self, payload: GuardrailPayload, direction: Direction) -> GuardrailVerdict: ...
```

- `Direction`: `PRE` (request) | `POST` (response). Same provider may be wired to
  one or both; a provider declares which directions it supports.
- `GuardrailPayload`: the extracted, inspectable text (reuse the routing
  text-extraction helper — last user message + system prompt, multimodal blocks
  flattened, `domain/routing.py`) plus the raw operation body so a redacting
  provider can rewrite it. Immutable (frozen dataclass).
- `GuardrailVerdict` (frozen): `action` = `ALLOW | REDACT | BLOCK`,
  `categories: tuple[str, ...]` (matched policy categories, e.g. `pii.email`,
  `moderation.violence`), `redacted_payload: GuardrailPayload | None` (set only
  for `REDACT`), `provider_id`, `latency_ms`. `signals`-style human-readable
  notes as in `RoutingDecision`.

The registry mirrors `application/routing/service.py:81` (`STRATEGIES`): a
`GUARDRAILS: dict[str, type[GuardrailProvider]]` keyed by provider id, each
constructed from its slice of config. A team/model configures an **ordered
chain** of guardrails per direction (pre-chain, post-chain).

### 2. Config surface

A `GuardrailPolicy` (frozen) attached to a `Team` and optionally overridden per
`Model` (mirror how `Model.params_enforced`/`max_output_tokens` layer over team
defaults, `domain/entities/model.py:51,55`):

```json
{
  "pre": [
    {"provider": "pii_regex", "action": "redact", "fail": "open",
     "config": {"categories": ["email", "phone", "credit_card", "iban", "api_key", "codice_fiscale"]}},
    {"provider": "moderation_llm", "action": "block", "fail": "closed",
     "config": {"model": "moderation-fast", "categories": ["violence", "self_harm"]}}
  ],
  "post": [
    {"provider": "pii_regex", "action": "redact", "fail": "open", "config": {...}}
  ]
}
```

`action` is the **policy** the admin assigns to a provider's positive match
(`allow`/`redact`/`block`) — a provider may *detect* more than the admin chooses
to *act on*. `fail` is the failure policy (§5). Empty chains = off (default).
Store this as config, never hardcode categories.

### 3. Three built-in providers (implement in this order)

#### P1 — Regex/rule-based PII (`application/guardrails/pii_regex.py`)

Pure, no I/O, no ML. Detects and redacts, keyed by category:

- **email**, **phone** (E.164 + IT formats), **IBAN** (mod-97 checksum),
  **credit card** (Luhn check — reject false positives on any 16-digit run),
  **API keys / secrets** (high-entropy tokens, `sk-…`, bearer patterns), and
  **IT codice fiscale** (16-char pattern + control-char check — a nod to the
  codebase's existing IT bias, cf. the EN+IT keyword lists in
  `application/routing/complexity.py`).
- Redaction replaces each match with a stable typed placeholder
  (`[REDACTED:email]`), never the raw value. Verdict carries categories + counts,
  never the matched substrings (see non-goals — no raw PII in logs).
- Checksum-gated detectors (Luhn/IBAN/CF) keep false positives low; this is the
  only provider that can run with **zero added latency** and is the default P1.

#### P2 — LLM / native moderation (`application/guardrails/moderation.py`)

Two backends behind the same provider:

- **Gateway-model moderation**: send the extracted text to a configured
  moderation model *through the gateway's own `LLMGateway` port*
  (`domain/ports/llm_gateway.py`, `achat_completion` with constrained
  `response_format` json_schema — reuse `infrastructure/llm/structured_output.py`
  exactly as the S4 judge does). The moderation model returns a category→score
  map; the provider maps scores over a threshold to matched categories.
- **Provider-native `/v1/moderations`**: for OpenAI-family credentials, call the
  native moderations endpoint directly (cheaper, purpose-built). Selected by
  config; same verdict shape out.

Give it a hard time budget (configurable, default 2000 ms, as routing's
network strategies do). It is the primary **blocking** guardrail.

#### P3 — Webhook (`application/guardrails/webhook.py`)

BYO policy engine, mirroring the routing webhook contract exactly
(`docs/routing-webhook.md`, `application/routing/webhook.py`). Contract
documented in `docs/guardrails-webhook.md`:

```text
POST <url>              timeout: configurable, default 2000 ms
{ "direction": "pre", "text": "<extracted>", "categories_requested": [...],
  "metadata": { "team": "...", "model": "..." } }

→ 200 { "action": "block" }
       { "action": "redact", "redacted_text": "..." }
       { "action": "allow" }
```

Strict Pydantic validation at the boundary; malformed/out-of-range/non-2xx/
timeout → failure policy (§5), never a silent pass.

### 4. Pre-call vs post-call semantics & placement

- **Pre-call** runs in `CompletionService._prepare` (line 497) **after**
  `sanitize_request` (already applied per-op, e.g. `chat_completion` line 581) and
  `clamp_output_tokens` (line 566), and **before** `_meter.admit` (line 567).
  Rationale: redaction changes the text, which changes the token estimate the
  reservation is built from, so redaction must precede admission; and a `BLOCK`
  must not reserve budget for a call that will never happen. On `BLOCK`, raise a
  new `GuardrailBlocked(DomainError)` (add to `domain/exceptions.py` beside
  `UnsupportedOperation` line 170) → the controller maps it to a clear 4xx; the
  provider is never called. On `REDACT`, replace the operation body with the
  redacted payload and proceed. On `ALLOW`, continue untouched.
- **Post-call** runs in `_dispatch` (line 135) on the returned `response`
  **before** `settle_ok`. A blocking post-verdict replaces the model output with
  the domain error (the call already happened → usage is still metered and
  billed; document that a blocked response is a *paid* block). Redaction rewrites
  the response body in place before returning to the caller.
- Ordering vs the param allowlist/clamp is fixed and one-directional:
  `sanitize_request` → `clamp_output_tokens` → **pre-guardrails** → `admit` →
  dispatch → **post-guardrails** → `settle_ok`. Guardrails never see transport
  overrides (already stripped) and always see the clamped body.

### 5. Fail-open vs fail-closed (SECURITY-sensitive)

Every guardrail declares a `fail` policy; the default is the **safe** one for its
action:

- A **blocking** guardrail (moderation) **fails closed** by default: if the
  provider errors or times out, the request is **blocked** (`GuardrailBlocked`),
  not allowed through unscreened. A moderation outage must not become an open
  door.
- A **redacting** guardrail **fails open** by default: a PII-redactor outage
  degrades to proceeding *with a logged warning and an audit entry*, because
  failing closed on redaction turns a best-effort hygiene layer into an
  availability risk. Admins who want strict privacy set `fail: "closed"`.

Both are **explicit and configurable per guardrail** — never inferred silently.
This is the mirror of routing's §4 "failure never fails the user request", but
inverted for the security-critical case: here, for blocking guardrails, failure
*does* fail the request, by design. A regression test must pin each direction
(see the plan).

### 6. Streaming

Post-call moderation on a stream is genuinely hard — there is no complete
response to inspect until the last chunk. Two documented modes, chosen by config:

- **Block-streaming-when-active** (default, safe): if a *blocking* post-guardrail
  is configured for a model, disable SSE for that model's requests (or buffer the
  full response server-side, moderate, then emit non-streamed). Simple, correct,
  higher latency-to-first-token.
- **Delay-window incremental scan**: buffer a sliding window of N chunks, scan
  the accumulated text on each flush, release chunks that clear the window. Bounds
  leakage to the window size; more complex, opt-in. Redaction-only post-guardrails
  can scan-and-rewrite per window without blocking. Wire this through `_metered`
  (line ~665) / the metered generator, never by unwinding an already-emitted
  chunk.

### 7. Audit-log integration

Every `BLOCK` and every `REDACT` is an auditable event via the existing
append-only `AuditLog` port (`domain/ports/audit.py`, `stage`/`record`), action
`guardrail.block` / `guardrail.redact`, target = model/team, `detail` = matched
**categories and counts only**. The `AuditEvent` entity is explicit: *"Never
store secrets in `detail`"* (`domain/entities/audit.py`) — so redact before you
log, always. `ALLOW` is not audited (too noisy); expose counters via the usual
observability path instead.

### 8. Performance

Guardrails add latency on the critical path. Budget it: P1 regex is sub-ms and
free; P2/P3 are network calls with a hard timeout (default 2000 ms). Within a
direction's chain, **run independent checks in parallel** (`asyncio.gather`) and
combine verdicts (any `BLOCK` wins; `REDACT`s compose left-to-right). A blocking
provider short-circuits the rest of its chain once it has enough to block. Record
per-provider `latency_ms` on the verdict for the observability endpoints.

---

## Console exposure

Surface the policy in the admin UI (plan 03): per-team and per-model guardrail
chains, each provider's action + fail policy, and a read-only feed of recent
`guardrail.block`/`guardrail.redact` audit events with categories. Secret
material in webhook config (`bearer_token`) is masked exactly like routing's
`BEARER_TOKEN_MASK` (`domain/routing.py`).

## Explicit non-goals (do not build)

- **Do not build your own ML moderation model.** P2 calls an existing moderation
  model or a provider-native endpoint; training/serving a classifier is out of
  scope.
- **Do not persist raw PII or matched secrets anywhere** — not in logs, not in
  audit `detail`, not in traces. Redact before logging; store categories/counts
  only.
- No per-user allow/deny lists, no jailbreak-detection heuristics, no automatic
  prompt rewriting beyond redaction — documented future work.

## Delivery

Phased, each independently shippable and tested (see `plans/06-guardrails.md`):
(0) port + verdict types + pipeline hook points; (1) regex PII provider;
(2) LLM/native moderation; (3) webhook + console config.
