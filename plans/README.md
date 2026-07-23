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
| 04 | [Response caching](04-response-caching.md) | ⏳ designed, not started | Cost & latency — exact-match + optional semantic cache, per-tenant isolated |
| 05 | [Cross-provider failover](05-cross-provider-failover.md) | ⏳ designed, not started | Reliability — fall over to another capable candidate on 429/5xx/timeout |
| 06 | [Guardrails](06-guardrails.md) | ⏳ designed, not started | Enterprise policy — pluggable PII/moderation pre- and post-call |
| 07 | [Budget alerts](07-budget-alerts.md) | ⏳ designed, not started | Proactive spend notifications at % thresholds, off the hot path |
| 08 | [Extended endpoints](08-extended-endpoints.md) | ⏳ designed, not started | Surface breadth — audio, moderations, rerank, Batch/Files |
| 09 | [Responses API Level B](09-responses-level-b.md) | ⏳ designed, not started | Contract correctness — faithful tool events on chat-only providers, fail loudly otherwise |
| 10 | [Usage analytics](10-usage-analytics.md) | ⏳ designed, not started | Accurate streaming savings + temporal cost/token/call charts |
| 11 | [Platform quality gates](11-platform-quality-gates.md) | ⏳ planned, not started | Request correlation, drift gates, browser E2E, dependency safety |
| 12 | [Routing evolution](12-routing-evolution.md) | ⏳ designed, not started | Capability discovery, shadow promotion, dry-run simulation, native-family routing |
| 13 | [Billing integrity & retention](13-billing-integrity.md) | ⏳ designed, not started | Image/cache-token pricing, decimal money, distributed budgets, durable history |

## Recommended order

1. **Correctness now:** Plan 09 Phase 0 (stop silently accepting unsupported
   Responses fields), then the remaining Level B tool contract; Plan 10 Phase 0
   (attach stream usage to routing decisions).
2. **Trust the delivery pipeline:** Plan 11's OpenAPI/migration drift gates and
   critical Playwright flows. Request correlation can ship independently.
3. **Money correctness:** Plan 13 image/cache-token pricing and decimal ledger
   before adding more billing-dependent surfaces; Plan 10 time-series API/UI can
   then build on the authoritative data.
4. **Reliability and policy:** sequential failover → observability → circuit
   breaker in Plan 05, with guardrails (Plan 06) parallel once its policy
   contract is settled.
5. **Product expansion:** Plans 07, 04, 12 and 08. Response caching remains
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
