# Execution plans

This directory holds the **execution roadmap** for the gateway — the sequenced,
task-level "how and in what order" for upcoming work.

It is deliberately separate from `docs/next-steps/`:

- `docs/next-steps/*.md` = **design & rationale** (what the feature is, why, the
  non-negotiable constraints). Written before implementation.
- `plans/*.md` = **execution plan** (phases, concrete tasks, file touchpoints,
  success criteria, risks, test strategy). Each plan links back to its design doc.

## Status snapshot (as of this plan)

- Round 1–7 review findings: **all HIGH + MEDIUM fixed and merged**; all six
  Round 7 LOWs (L35–L40) fixed and merged.
- `main` is green: full suite passing, `ruff` clean, `pyrefly` 0 errors, all
  pre-commit hooks pass. CI runs the suite on SQLite **and** a real Postgres job
  (`alembic upgrade head` + persistence subset), plus a Docker build + `/health`
  smoke test; Dependabot watches `uv` + `github-actions` weekly.
- Migration chain validated end-to-end against Postgres; schema drift reconciled.
- **Plan 01 shipped:** native Anthropic `/v1/messages` and Gemini `generateContent`
  endpoints (non-streaming + streaming), real-SDK-validated, documented, conformance-
  locked. **Plan 02 Phase 1–2 shipped** (OpenAI contract + error-envelope parity);
  Phase 3 (client docs + surface-selection note) is the current work.

## The roadmap

| # | Plan | Status | Theme |
|---|------|--------|-------|
| 01 | [Native provider endpoints](01-native-provider-endpoints.md) | ✅ **complete** (Anthropic + Gemini) | Product differentiator — native SDKs point at the gateway |
| 02 | [Framework-agnostic wire-contract conformance](02-agent-frameworks.md) | ✅ Phase 1–2 + native contracts; ⏳ Phase 3 docs | Any client speaking the wire spec works — validated by contract, not per-framework |
| 03 | [Admin UI](03-admin-ui.md) | ⏳ not started (`ui/` empty) | Non-dev operability (teams, budgets, keys, usage) |

**Recommended order:** 02 phase 1 (OpenAI Chat Completions **contract conformance**
on the existing surface) first — it ships value now, locks current behavior, and
becomes the acceptance layer for 01. Then 01 phase 1 (Anthropic `/v1/messages`) →
validate with the native SDK → 01 phase 2 (Gemini), extending the same conformance
harness to the native contracts as its acceptance. Plan 03 (UI) is
backend-independent and can run in parallel whenever capacity allows.

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

## Backlog / follow-ups (not yet scheduled)

- **Pagination tiebreaker, remaining repos.** L36 fixed the six named repos;
  `repository.py` (APIKey), `organization_repository.py`, `secret_key_repository.py`,
  `scim_token_repository.py`, `team_repository.py`, `usage_repository.py` share the
  same missing-`id`-tiebreaker pattern. Low risk; a one-PR sweep closes it.
- **Native-endpoint smart routing (phase 3).** Routing is intentionally out of
  scope for native endpoints in plan 01 phase 1–2; revisit once native usage
  metering is proven.
- **`mlflow` upper-bound pin.** Pinned `>=3.14` with no ceiling; the new
  `MLflowTraceSink` test gives a signal, but consider capping the major version.
