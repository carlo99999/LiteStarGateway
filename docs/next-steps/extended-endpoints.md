# Implementation prompt: Extended OpenAI-compatible endpoints

You are extending this gateway's OpenAI-compatible surface (Litestar, hexagonal
architecture, `src/litestar_gateway/`) with the endpoints the ecosystem expects
but the gateway does not yet expose: **audio transcriptions/translations**,
**moderations**, **rerank**, and the **Batch API** (+ **Files**). Every new
endpoint flows through the *same* governed pipeline as the existing ones — auth →
per-IP/per-key rate limit → budget admission → sanitize → adapter dispatch →
metering — per provider capability. No new middleware, no forked money path.

Read `CLAUDE.md`, `CONTRIBUTING.md`, and the existing surface first:
`infrastructure/web/api_router/completions.py`, `.../router.py`,
`domain/ports/llm_gateway.py`, `infrastructure/llm/gateway.py`,
`infrastructure/llm/feature_support.py`, `application/completion_service.py`, and
`application/usage_meter.py`. Follow the conventions already in place: Protocol
ports in `domain/`, services in `application/`, adapters in `infrastructure/`,
frozen dataclasses, full typing, DI everywhere, many small files, TDD.

---

## The pattern you are extending (non-negotiable)

Adding an endpoint today means three surgical touches, proven by
`/v1/embeddings` and `/v1/images/generations`:

1. **Port method** on `LLMGateway` (`domain/ports/llm_gateway.py:86-100` —
   `embeddings`/`aembeddings`/`images`/`aimages`). Add the new async method(s).
2. **Capability slot + dispatch** in `LLMGatewayImpl`
   (`infrastructure/llm/gateway.py:41-73`): each provider entry is
   `(adapter, frozenset({...operations}))`; `_resolve`
   (`gateway.py:75-82`) raises `UnsupportedOperation` when the operation is not
   in the provider's set. That exception maps to **HTTP 501** in
   `infrastructure/web/exception_handlers.py:132` — this *is* the clean-501
   mechanism, reused unchanged.
3. **Adapter method** per capable provider (e.g. `openai_adapter.py:178-202`),
   plus a service method on `CompletionService` that calls `_prepare` →
   `_dispatch` (`completion_service.py:497,135,691`) and an endpoint handler
   registered on the protected `api_router` (`router.py:44-58`).

The service methods differ only in the operation string, the `ModelType`
(`domain/entities/enums.py:72-75` — `CHAT`/`IMAGE`/`EMBEDDINGS`), and the
sanitizer allowlist (`request_policy.py:23-85`). Governance is already uniform:
`_prepare` enforces the key rate limit, resolves+guards the model, clamps output
tokens, and calls `UsageMeter.admit` for the budget reservation
(`usage_meter.py:239`); `_dispatch` runs the call, settles usage via
`settle_ok`/`_parse_usage` (`usage_meter.py:313,147`), and releases the
reservation in `finally`. **Each new endpoint reuses this spine**; only the
metering *shape* and the sanitizer entry change.

---

## Endpoint 1 — Audio: `POST /v1/audio/transcriptions` + `/v1/audio/translations`

- **New `ModelType.AUDIO`** (`entities/enums.py:72`) so an audio model can't be
  called on chat and vice-versa (`_ensure_usable`, `completion_service.py:192`).
- **Multipart, not JSON.** Unlike every existing handler (which binds
  `data: dict`), audio takes a `multipart/form-data` upload (`file`, `model`,
  `language`, `prompt`, `response_format`, `temperature`). The endpoint reads the
  file within the existing `MAX_BODY_SIZE` cap — `request_max_body_size`
  (`app.py:143`, default 10 MB, `config.py:24`) already 413s an oversized body
  *before* the handler, and that 413 is rendered in the OpenAI error envelope by
  the router's `HTTPException` handler (`router.py:60-67`). Document that large
  audio needs `MAX_BODY_SIZE` raised; do not add a second cap.
- **Sanitizer:** add `"audio.transcriptions"` / `"audio.translations"` allowlists
  to `_ALLOWED` (`request_policy.py:23`). The file bytes are passed positionally
  to the adapter, not through the field allowlist.
- **Provider support:** OpenAI + Azure (`client.audio.transcriptions.create`).
  Others → 501 via the capability set. Add `audio.transcriptions` /
  `audio.translations` to the OpenAI and Azure frozensets only.
- **Metering shape (new):** audio is billed by **duration (seconds)**, not
  tokens (some models also report tokens). `_parse_usage` (`usage_meter.py:147`)
  only knows token keys, so add an audio branch: introduce
  `audio_seconds_cost_per_second` on `Model` (mirrors `input_cost_per_token`) and
  a duration→cost path. Reservation (`_reservation_cost`, `usage_meter.py:166`)
  can't estimate duration pre-call from a token count; reserve a flat
  configurable ceiling (or zero, settling on the authoritative post-call
  duration) — state which and why. Translations bill identically to
  transcriptions.
- **Streaming:** optional and last — OpenAI streams transcription events; relay
  them with the existing SSE plumbing (`completions.py:37`) only if trivial.

## Endpoint 2 — `POST /v1/moderations`

- **Reuses `ModelType.CHAT`** or a dedicated `MODERATION` type (prefer a
  dedicated type for clean `GET /v1/models` discovery — see cross-cutting).
- **Metering shape (new): usually zero-cost.** OpenAI moderation is free; model
  `input_cost_per_token`/`output_cost_per_token` default `None`→`0.0`
  (`usage_meter.py:160`), so `settle_ok` already bills $0 while still recording a
  usage event + trace for observability. No special-casing needed beyond leaving
  costs unset; a priced moderation model (rare) bills by input tokens through the
  existing path. **Still runs `admit`** so the request counts against RPM and is
  traced.
- **Provider support:** OpenAI + Azure. Others → 501. Add `moderations` to those
  two frozensets and a `moderations`/`amoderations` adapter method
  (`client.moderations.create`).
- **Guardrails synergy (cross-reference, not a dependency):**
  `docs/next-steps/guardrails.md` layers moderation
  *inline* on chat requests as a policy gate. This endpoint is the standalone
  OpenAI surface for the same provider capability; if guardrails is built, both
  call one shared `amoderations` port method — design the adapter method so the
  guardrails path can reuse it. Until then, `/v1/moderations` stands alone.

## Endpoint 3 — `POST /v1/rerank`

- **Not native OpenAI** — a common gateway convention (Cohere Rerank, Databricks,
  Bedrock rerank). Define the wire shape explicitly:

  ```text
  POST /v1/rerank
  { "model": "<alias>", "query": "<text>",
    "documents": ["doc a", "doc b", ...], "top_n": 3 }
  → { "results": [ { "index": 0, "relevance_score": 0.98 }, ... ],
      "usage": { "input_tokens": 1234 } }
  ```

- **New `ModelType.RERANK`.** Sanitizer allowlist `{"model","query","documents",
  "top_n","return_documents"}` (`request_policy.py:23`).
- **Provider support:** the providers with a rerank API — Bedrock
  (`bedrock_adapter.py`), Databricks/Cohere via the OpenAI-compatible client, and
  Vertex where available. OpenAI/Azure/Anthropic → 501. Add `rerank` only to the
  capable providers' frozensets.
- **Metering shape:** by **input tokens** (query + documents). This fits
  `_parse_usage` directly when the provider returns a `usage` block; when it does
  not, the H14 estimation fallback in `settle_ok` (`usage_meter.py:330`) estimates
  from the request text — `_request_text` (`usage_meter.py:44`) must be taught the
  `query`/`documents` fields, or a rerank-specific estimator supplied.

## Endpoint 4 — Batch API: `POST /v1/batches` + `POST /v1/files` (heaviest — stage last)

Fundamentally different: **asynchronous and stateful**. It does not fit the
synchronous `admit → dispatch → settle → release` spine, so it is the largest
slice and may be deferred or shipped in its own multi-phase effort.

- **Files first.** `POST /v1/files` (purpose `batch`) uploads a JSONL of requests;
  `GET /v1/files/{id}`, `GET /v1/files/{id}/content`, `DELETE`. Needs **durable
  file storage** (a new `FileStore` port + adapter — local/S3) and a `file` table
  (Alembic migration, `docs/db-migrations.md`), scoped to the team like every
  other resource. Respect `MAX_BODY_SIZE`.
- **Batch job model.** `POST /v1/batches` references an uploaded `input_file_id`,
  an endpoint (`/v1/chat/completions`, `/v1/embeddings`), and a completion window.
  Needs a `batch` table (status: `validating`→`in_progress`→`completed`/`failed`/
  `expired`), a background worker that fans out the file's rows through the
  *existing* `CompletionService` per line (so each sub-request is auth/budget/
  meter-governed exactly like a live call), and `GET /v1/batches/{id}` polling +
  `POST /v1/batches/{id}/cancel`.
- **Budget over a deferred job (new shape).** You cannot reserve a precise cost
  up front. Options, pick and document one: (a) **reserve at completion** — meter
  each fanned-out sub-request as it runs through `CompletionService` (simplest,
  reuses everything, but a batch can overrun budget mid-run → cancel remaining
  rows when `admit` first raises `BudgetExceeded`); (b) reserve a pessimistic
  ceiling from the input file's estimated tokens at create time and reconcile at
  completion. Metering is **at completion per row**, not per HTTP call.
- **Provider support:** only providers with real batch APIs (OpenAI, Azure) get a
  *native* batch; for others the gateway's own fan-out worker emulates it over
  their sync endpoints, or returns 501 — decide per provider and reflect it in the
  matrix.

---

## Cross-cutting

- **Capability matrix / clean 501.** Every new operation is a string added to the
  per-provider frozenset in `gateway.py:41-73`; an incapable provider →
  `UnsupportedOperation` → 501 (`exception_handlers.py:132`), matching how
  Anthropic embeddings already 501s (README matrix, `README.md:116`). The
  `feature_support.py` guard is orthogonal (it rejects untranslatable *chat*
  features); no changes needed there unless audio/rerank grow translation layers.
- **`GET /v1/models` discovery.** `list_models` (`models_list.py:38`) already
  emits each callable's `type`; new `ModelType`s surface automatically so clients
  can see which alias serves audio/rerank/moderation. No handler change beyond the
  new enum values.
- **Consistent error envelope.** All handlers register on `api_router`, inheriting
  the OpenAI error-envelope handlers (`router.py:59-66`) — new endpoints get the
  right shape for free (400/401/413/429/501).
- **Per-endpoint metering summary.** audio → seconds (new cost field);
  moderations → $0 (unset costs, existing path); rerank → input tokens (existing
  path + estimator tweak); batch → per-row at completion (existing per-sub-request
  metering, new reservation policy).
- **Threading auth/limit/budget/sanitize.** Synchronous endpoints (audio,
  moderations, rerank) add one `CompletionService` method each that calls
  `_prepare`→`_dispatch`, so they inherit the full spine unchanged. Batch threads
  governance per fanned-out sub-request, not per batch HTTP call.

## Explicit non-goals

- Fine-tuning, Assistants/threads, vector stores, realtime/websocket audio,
  speech **synthesis** (`/v1/audio/speech`) — documented future work only.
- A bespoke rerank ranking algorithm — the gateway proxies provider rerank, it
  does not rank.
- Cross-provider batch translation beyond the simple fan-out-over-sync emulation.
- Building guardrails here — moderation-as-a-policy-gate is its own design doc.
