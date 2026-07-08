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

## The roadmap

| # | Plan | Depends on | Theme |
|---|------|-----------|-------|
| 01 | [Native provider endpoints](01-native-provider-endpoints.md) | H23 (done) | Product differentiator — native SDKs point at the gateway |
| 02 | [Agent-framework compatibility](02-agent-frameworks.md) | 01 | Tool-calling agents (Pydantic AI, LangChain, OpenAI Agents) work end-to-end |
| 03 | [Admin UI](03-admin-ui.md) | backend only | Non-dev operability (teams, budgets, keys, usage) |

**Recommended order:** 01 phase 1 (Anthropic `/v1/messages`) → validate with the
native SDK → 01 phase 2 (Gemini) → 02. Plan 03 (UI) is backend-independent and can
run in parallel whenever capacity allows.

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
