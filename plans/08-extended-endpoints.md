# Plan 08 — Extended OpenAI-compatible endpoints

**Design doc:** [`docs/next-steps/extended-endpoints.md`](../docs/next-steps/extended-endpoints.md)
**Depends on:** the existing governed pipeline only — auth/rate-limit/budget/
sanitize/metering (`application/completion_service.py`, `application/usage_meter.py`)
and the capability matrix (`infrastructure/llm/gateway.py:41-73`) are all in place.
No dependency on plans 01–03.
**Theme:** finish the OpenAI-compatible surface — audio, moderations, rerank, and
the Batch API — each new endpoint flowing through the *same* governed pipeline as
`/v1/embeddings`, per provider capability, so unsupported provider→endpoint pairs
return a clean **501** exactly like Anthropic embeddings do today.

**Status: ⏳ not started.**

## Guardrails (from the design doc — non-negotiable)

1. **Reuse the three-touch pattern, don't fork.** Each synchronous endpoint =
   one `LLMGateway` port method + a capability slot in `LLMGatewayImpl` + an
   adapter method per capable provider + a `CompletionService` method
   (`_prepare`→`_dispatch`) + a handler on `api_router`. No new middleware.
2. **Clean 501 via the capability set.** Add the operation string only to the
   frozensets of providers that serve it (`gateway.py:41-73`); `_resolve`
   (`gateway.py:75`) raises `UnsupportedOperation` → 501
   (`exception_handlers.py:132`) for the rest.
3. **Meter through `UsageMeter`.** Reuse `admit → settle_ok → release`; only the
   cost *shape* changes per endpoint (seconds / $0 / tokens / per-row).
4. **Respect `MAX_BODY_SIZE`.** Uploads (audio, files) ride the existing
   `request_max_body_size` cap (`app.py:143`); no second cap.

## Phase 1 — Audio transcriptions + translations (1 slice, independently shippable)

- `ModelType.AUDIO` (`entities/enums.py:72`); `Model.audio_cost_per_second`
  (Alembic migration, `docs/db-migrations.md`).
- Multipart handler `POST /v1/audio/transcriptions` (+ `/v1/audio/translations`)
  on `api_router` — form binding, not `data: dict`.
- Sanitizer allowlists `audio.transcriptions`/`audio.translations`
  (`request_policy.py:23`); `audio` frozenset entries on OpenAI + Azure only;
  `amoderations`-style adapter methods (`client.audio.transcriptions.create`).
- Metering by duration: an audio branch alongside `_parse_usage`
  (`usage_meter.py:147`); reservation policy per design doc (flat ceiling or
  settle-only).
- **Done when:** an OpenAI/Azure audio model transcribes and translates a file,
  bills by duration, records a usage event + trace; a non-capable provider →501;
  an over-`MAX_BODY_SIZE` upload →413 in the OpenAI envelope. Streaming is a
  follow-up, not a blocker.

## Phase 2 — Moderations (1 slice)

- `ModelType.MODERATION`; handler `POST /v1/moderations`; sanitizer allowlist
  `{"model","input"}`; `moderations` frozenset entries on OpenAI + Azure;
  `moderations`/`amoderations` adapter method.
- `CompletionService.moderations` = `_prepare`→`_dispatch`, costs unset so
  `settle_ok` bills $0 (`usage_meter.py:160`) while still tracing.
- Design the `amoderations` port method so a future guardrails policy-gate reuses
  it (design doc cross-ref; no code dependency yet).
- **Done when:** OpenAI/Azure moderation returns flags, bills $0, records a trace;
  incapable provider →501; the request still consumes RPM + budget admission.

## Phase 3 — Rerank (1 slice)

- `ModelType.RERANK`; handler `POST /v1/rerank` with the documented request/
  response shape; sanitizer allowlist `{"model","query","documents","top_n",
  "return_documents"}`.
- `rerank` frozenset entries on the capable providers only (Bedrock, Databricks/
  Cohere, Vertex where available); OpenAI/Azure/Anthropic →501.
- Adapter method per capable provider; teach `_request_text`
  (`usage_meter.py:44`) the `query`/`documents` fields so the H14 estimate
  (`usage_meter.py:330`) works when a provider omits `usage`.
- **Done when:** a capable provider reranks documents, bills by input tokens
  (authoritative or estimated); OpenAI/Anthropic →501.

## Phase 4 — Batch API + Files (large, deferrable — its own multi-phase effort)

Split further; the first two sub-slices are prerequisites for the rest.

- **4a — Files.** `FileStore` port + adapter (local/S3), `file` table + migration,
  `POST /v1/files` / `GET` / `GET .../content` / `DELETE`, team-scoped,
  `MAX_BODY_SIZE`-bounded. Done when a JSONL uploads, is retrievable, and is
  isolated per team.
- **4b — Batch model.** `batch` table + migration; `POST /v1/batches` referencing
  an `input_file_id`; `GET /v1/batches/{id}` polling; `POST .../cancel`. Done when
  a batch persists its lifecycle states and is queryable.
- **4c — Fan-out worker.** Background worker reads the input file and dispatches
  each row through the existing `CompletionService`, so every sub-request is
  auth/budget/meter-governed; metering is per-row at completion; writes an output
  file. Budget policy per design doc (meter-per-row, cancel remaining on first
  `BudgetExceeded`). Done when a completed batch produces a downloadable output
  file with correct per-row billing and cancels cleanly when budget is exhausted.
- **4d (optional) — Native batch.** Route OpenAI/Azure batches to the provider's
  real batch API; others stay on the fan-out emulation or 501.
- **May be deferred entirely** past phases 1–3 without blocking them.

## TDD strategy (per endpoint, following plan 01)

- **Unit:** the adapter request/response mapping (table-driven where natural).
- **Integration through the endpoint with a fake adapter** proving governance +
  metering applies — the fake records the usage shape; assert `admit`/`settle_ok`/
  `release` fired and the reservation released (mirrors the completion tests).
- **501 test:** an incapable provider→endpoint returns 501 in the OpenAI envelope.
- **Real-SDK conformance where feasible (plan 01 pattern):** `OpenAI(base_url=…)`
  `.audio.transcriptions.create` / `.moderations.create` / `.files` / `.batches`
  against the gateway; rerank has no OpenAI SDK surface → assert the documented
  wire contract directly.
- **Audio:** a 413 test for an over-`MAX_BODY_SIZE` upload. **Batch:** a
  budget-overrun test (fan-out cancels remaining rows).

## Risks & mitigations

- **Multipart body-size** (audio, files): rely on `request_max_body_size`
  (`app.py:143`) — one 413 test per upload endpoint; document raising
  `MAX_BODY_SIZE` rather than adding a second cap.
- **Async batch complexity** (biggest risk): isolate in phase 4, ship 1–3 first;
  fan out through the *existing* `CompletionService` so batch adds no parallel
  money path; the deferred-budget policy is the one genuinely new mechanism —
  gate it behind the budget-overrun test.
- **Uneven provider support:** the capability frozensets are the single source of
  truth; a matrix test per endpoint asserts exactly which providers 200 vs 501,
  and the README matrix (`README.md:112-117`) is updated in the same slice.
- **Metering-shape drift:** audio (seconds) and batch (per-row) don't fit
  `_parse_usage`'s token model — add narrow branches, unit-test each against a
  known cost, and assert $0 for moderations.

## Execution / slicing

- Phases 1–3 are **independently shippable and parallelizable**: each touches a
  disjoint sanitizer entry, `ModelType`, and adapter method. They *share*
  `gateway.py` (frozenset edits), `enums.py` (new `ModelType`s), `request_policy.py`
  (`_ALLOWED`), and `usage_meter.py` — group those shared-file edits per slice or
  serialize the merges to avoid conflicts (per `plans/README.md` conflict-graph
  guidance).
- Phase 4 is a separate track: 4a→4b→4c sequential (each builds on the prior);
  4a/4b can start in parallel with phases 1–3 since they add new tables/ports, not
  shared edits. Run the full gate on merged `main` after any parallel batch.
