# Execution plans

This directory holds the **execution roadmap** for the gateway — the sequenced,
task-level "how and in what order" for upcoming work.

It is deliberately separate from `docs/next-steps/`:

- `docs/next-steps/*.md` = **design & rationale** (what the feature is, why, the
  non-negotiable constraints). Written before implementation.
- `plans/*.md` = **execution plan** (phases, concrete tasks, file touchpoints,
  success criteria, risks, test strategy). Each plan links back to its design doc.

## Status snapshot

- Round 1–12 review findings: **fully remediated**. The two Round 12 MEDIUMs
  were fixed by #332/#333; no reviewed finding remains open.
- `main` is green: full suite passing, `ruff` clean, `pyrefly` 0 errors, all
  pre-commit hooks pass. CI runs the suite on SQLite **and** a real Postgres job
  (`alembic upgrade head` + full suite), plus a Docker build + `/health`
  smoke test; Dependabot watches `uv` + `github-actions` weekly.
- Migration chain validated end-to-end against Postgres; schema drift reconciled.
- **Plan 01 shipped:** native Anthropic `/v1/messages` and Gemini `generateContent`
  endpoints (non-streaming + streaming), real-SDK-validated, documented, conformance-
  locked. **Plan 02 shipped:** Level A contract, native contracts, client guide and
  surface-selection note. **Plan 03 shipped:** the full admin console is served at
  `/ui`, including DB-backed OIDC settings.

## The roadmap

| # | Plan | Status | Theme |
|---|------|--------|-------|
| 01 | [Native provider endpoints](01-native-provider-endpoints.md) | ✅ **complete** (Anthropic + Gemini) | Product differentiator — native SDKs point at the gateway |
| 02 | [Framework-agnostic wire-contract conformance](02-agent-frameworks.md) | ✅ **complete** (Level A + native contracts + docs) | Any client speaking the wire spec works — validated by contract, not per-framework |
| 03 | [Admin UI](03-admin-ui.md) | ✅ shipped (full console at `/ui`) | Non-dev operability (teams, budgets, keys, usage) |
| 04 | [Response caching](04-response-caching.md) | ✅ Complete — exact-match, Redis-backed shared store selected by `REDIS_URL` + in-memory fallback, synthetic-stream replay for cached chat completions, semantic tier at cosine ≥ `RESPONSE_CACHE_SEMANTIC_THRESHOLD` tried only on an exact-match miss, off by default + per-team/model opt-in for both tiers, `cache_hit` metered at $0, plus console observability (`GET /teams/{team_id}/cache/savings` + `GET /cache/savings` hit rate/cost-saved endpoints, model-dialog toggle checkboxes, a `ModelsPage.tsx` savings panel) | Cost & latency — exact-match + optional semantic cache, per-tenant isolated |
| 05 | [Cross-provider failover](05-cross-provider-failover.md) | 🚧 Phase 0 + 1 + 2 + Phase 3 (observability + circuit breaker) complete (non-streamed and streaming failover, pre-first-byte only, config, RPM/budget single-charge guarantees, persisted attempts/failover_used, in-memory/Redis breaker skipping tripped candidates); only the admin-console reliability view remains open | Reliability — fall over to another capable candidate on 429/5xx/timeout |
| 06 | [Guardrails](06-guardrails.md) | ⏳ designed, not started | Enterprise policy — pluggable PII/moderation pre- and post-call |
| 07 | [Budget alerts](07-budget-alerts.md) | ✅ Complete — per-team `thresholds` (% of cap) + dedup ledger, settlement-time evaluation enqueuing a durable `pending_budget_alert` outbox, a background dispatch worker that resolves each alert's channel(s) from the owning team's budget, webhook (SSRF-guarded, per-team override of a platform target) and email (stdlib SMTP) `NotificationChannel`s, per-team config on `GET/PUT/DELETE /teams/{id}/budget` (thresholds + `alert_webhook_url`/`alert_email`, boundary-validated), a read-only `GET /teams/{id}/budget/alerts`, and a console Budgets **Alerts** section (thresholds + channel edit for platform admins, recent-fired-alerts list for all budget readers) | Proactive spend notifications at % thresholds, off the hot path |
| 08 | [Extended endpoints](08-extended-endpoints.md) | ⏳ designed, not started | Surface breadth — audio, moderations, rerank, Batch/Files |
| 09 | [Responses API Level B](09-responses-level-b.md) | 🚧 Phase 0 + 1a + provider Chat tool replay + streaming tool events (Databricks, Anthropic) + Vertex Responses tool calls complete; Bedrock streaming, Vertex streaming tool calls are accepted known limits | Contract correctness — faithful tool events on chat-only providers, fail loudly otherwise |
| 10 | [Usage analytics](10-usage-analytics.md) | ✅ Complete — routed streams attach settled usage to the routing decision identically to non-streamed calls; bucketed `GET /teams/{id}/usage/timeseries` (dialect-portable SQL bucketing, DST-independent UTC boundaries, optional `group_by=model` for multi-series in one call); console per-team cost/token/call stacked-area chart with date/granularity/filter controls, an accessible table fallback, distinct error/zero states, and an opportunistic budget-cap/cache-savings overlay | Accurate streaming savings + temporal cost/token/call charts |
| 11 | [Platform quality gates](11-platform-quality-gates.md) | ✅ Complete — all four independent slices landed. `X-Request-ID` middleware bound to structlog and propagated to trace/usage/audit/routing-decision records via `request_context.current_request_id()`, trusted-proxy-gated inbound acceptance, migration for the four new `request_id` columns; CI gates on `just migration-check`, an OpenAPI/TypeScript schema-drift diff, and an internal Markdown link checker for `plans/`/`docs/`; Playwright browser E2E (login, RBAC, org/team/API-key mutations, Usage/Budgets) runs against a deterministic bootstrapped-admin backend in the `ui` CI job, trace/screenshot on failure only; `mlflow-skinny` pinned `>=3.14,<4` plus a scheduled non-blocking next-major compatibility workflow | Request correlation, drift gates, browser E2E, dependency safety |
| 12 | [Routing evolution](12-routing-evolution.md) | ⏳ designed, not started | Capability discovery, shadow promotion, dry-run simulation, native-family routing |
| 13 | [Billing integrity & retention](13-billing-integrity.md) | 🚧 Phase 1 (non-token pricing) and Phase 5 (retention lifecycle) complete — image/cache-token pricing via one normalized pricing function; soft-delete/tombstone for teams with billed history, `GET /teams/{id}/export` (usage/audit/routing-savings dump), audited platform-admin-only `POST /teams/{id}/purge`; Phases 2–4 (decimal money, distributed reservations, per-key budgets) not yet started | Image/cache-token pricing, decimal money, distributed budgets, durable history |
| 14 | [Gateway hot-path throughput](14-hot-path-throughput.md) | ✅ **complete** (outcome 2: +21–49% non-streaming, streaming gate PASS in-network; PRs #348–#353) | Profile-driven client reuse and hot-path work toward 300 RPS |
| 14a | [Hot-path implementation steps](14a-hot-path-implementation.md) | ✅ execution log of 14 — all six steps recorded, incl. the proxy-artifact investigation | Registry, adapter adoption, measured hotspots, deployment tuning |
| 15 | [Post-throughput next steps](15-next-steps.md) | 🚧 Phases A+B complete (in-network loadgen, true ceilings measured, B-i stop-optimizing decided); Phase C feature sequencing next | Bridge from Plan 14 to the feature roadmap (09 → 05 → 04 → 07 → 10) |

## Recommended order

1. **Performance evidence first:** Plan 14 Phase 0–1 — make the deterministic
   1-worker/3-worker benchmark permanent, then profile and fix provider-client
   lifecycle if the measurement confirms it before changing billing or
   persistence semantics.
2. **Correctness now:** Plan 09 Phase 2 (implement the streaming event
   contract), while keeping generic Vertex Responses tools fail-closed until a
   safe signature-state contract is selected. Plan 10 Phase 0 (attach stream
   usage to routing decisions) is now complete.
3. **Trust the delivery pipeline:** Plan 11's OpenAPI/migration drift gates and
   critical Playwright flows. Request correlation can ship independently.
4. **Money correctness:** Plan 13 image/cache-token pricing and decimal ledger
   before adding more billing-dependent surfaces; Plan 10 time-series API/UI can
   then build on the authoritative data.
5. **Reliability and policy:** sequential failover → observability → circuit
   breaker in Plan 05, with guardrails (Plan 06) parallel once its policy
   contract is settled.
6. **Product expansion:** Plans 07, 04, 12 and 08. Response caching remains
   opt-in and should follow its tenant-isolation threat model; Batch/Files stays
   last because it introduces a durable asynchronous execution model.

Compatibility is **framework-agnostic by construction**: the gateway implements
standard wire protocols, so conformance is asserted against the protocol contract
(with official SDKs as canaries), never per framework.

## Execution conventions (proven this project)

- **One branch per slice**, TDD (write the failing test first, RED→GREEN).
- **Parallel worktrees** for independent slices — partition by the file-level
  conflict graph so branches never collide; group work that shares a file (or the
  Alembic migration head) into one branch.
- **Gate before every PR:** `just test` (full suite green — never weaken tests to
  pass), `just lint`, `just typecheck`, `just pre-commit`.
- **Hexagonal boundary is law:** `domain/` and `application/` must not import
  `infrastructure`, `litestar`, or `sqlalchemy`. Provider/persistence/framework
  concerns live in `infrastructure/`.
- **Verify the merged result, not just the branches:** after a parallel batch,
  run the full gate on the merged `main` — integration issues only appear there.
- For Postgres-affecting work, run `just test-postgres` locally before relying on
  CI.

## Small follow-ups

- **Pagination tiebreaker tail.** Most of the old sweep is complete. Add `id` as
  the deterministic secondary ordering in `secret_key_repository.py` and
  `scim_token_repository.py`, then extend the existing parametrized regression.
- **Dependency ceiling:** tracked as Plan 11-D.
