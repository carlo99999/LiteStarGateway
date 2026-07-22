---
name: code-review
description: Perform deep, adversarial, evidence-backed reviews of repositories, pull requests, commit ranges, releases, or substantial features and produce a single deduplicated Markdown report. Use when Codex is asked for a comprehensive code review, security or correctness audit, a new review round, a regression review against earlier reports, or verified findings across authorization and tenancy, async and concurrency, persistence and transactions, billing and business invariants, APIs and integrations, frontend behavior, tests, dependencies, CI/CD, and production readiness. Also use when the user explicitly asks to remediate verified findings with regression tests.
---

# Verified Code Review

## Operating contract

- Treat product code as read-only by default. Always write the review report artifact and also return the complete report in the response; do not change product code unless the user explicitly requests remediation.
- Prefer a few verified, consequential findings over a long list of suspicions. Accept zero new findings when the evidence supports that result.
- Verify every included finding against the current tree. Never promote grep output, style preferences, comments, or a single suspicious line into a finding.
- Follow repository instructions and preserve unrelated user changes.
- Report only commands and checks actually executed. Never imply that an unavailable, skipped, or failing gate passed.
- Redact secret values from notes, tool output, and reports. If a live secret is found, identify its location and type without reproducing it, recommend rotation, and inspect for related exposure.
- Do not access production systems, mutate databases, create commits, push branches, or open issues or pull requests without explicit authorization.
- Write the report in the user's language. If the user gives no language preference, follow the language used by prior review rounds or the repository documentation.

## Resolve scope and output

1. Use the repository or change range named by the user. Otherwise use the current repository and state the resolved root.
2. Resolve the review scope in this order:
   - the user-provided commit range, pull request, release, or feature set;
   - the delta since the latest historical review when its boundary is unambiguous;
   - the current working tree and `HEAD` when no narrower range can be established.
3. Read prior reports from user-provided locations first. Otherwise inspect clearly named existing review directories such as `issues/`, `reviews/`, `audit/`, or `docs/reviews/`. Do not search outside the repository unless asked.
4. Select the output location deterministically:
   - use an explicit output file first;
   - otherwise use an explicit output directory;
   - otherwise use the historical directory containing the highest valid round number, breaking ties by the greatest number of valid round reports and then lexicographically by repository-relative path;
   - otherwise use the first existing directory in this order: `issues/`, `reviews/`, `audit/`, `docs/reviews/`;
   - otherwise create `reviews/code-review/` under the repository root.
5. Reuse the historical filename pattern and choose the next integer after the highest unambiguous round number, starting at 1. For an implicit filename or explicit directory, advance `N` until `round-N.md` is unused. Use that same resolved `N` in the filename, report title, and any round metadata.
6. For an explicit file that already exists, overwrite only when the user authorizes it. Otherwise, if its name follows the round pattern, advance `N` and the filename together; for a custom filename, use the first available `<stem>-K<extension>` starting with `K = 1`, and state the resolved path in the response.
7. Always save the report and return the complete report in the response, including a link to the saved artifact.

State any assumption that materially changes the scope. Ask only when ambiguity would make the review misleading or cause an unsafe write.

## Build project context

Before searching for defects:

1. Read all applicable agent instructions and the repository's primary documentation.
2. Map the repository structure, languages, frameworks, entry points, deployment modes, trust boundaries, and external services.
3. Inspect configuration, dependency manifests and lockfiles, migrations, schemas, tests, CI/CD, containers, and production startup paths.
4. Trace the main user and administrative flows. Write down the security and business invariants that those flows must preserve.
5. Inspect relevant Git history and the current worktree. Distinguish committed code from uncommitted user changes.
6. Read every previous review report in scope and build a ledger containing each stable issue ID, title, affected surface, status, resolution evidence, and whether it was deferred, accepted by design, or refuted.

Do not re-report a historical issue unless the fix is incomplete, the defect was reopened, a new surface has the same defect class, or the current impact is materially different. In that case cite the earlier issue and explain the new evidence.

## Establish the technical baseline

Discover commands from project-owned configuration instead of guessing. Run the safe, relevant gates that the repository supports, including when available:

- unit, integration, and end-to-end tests;
- lint, formatting checks, type checking, and pre-commit hooks;
- frontend tests and production builds;
- dependency vulnerability audits;
- migration consistency checks against each supported database class;
- tracked-secret scanning with values redacted;
- architecture or dependency-boundary checks;
- container or production-configuration validation.

Record the exact command, result, duration when useful, and any environmental limitation. A passing suite is evidence about covered behavior, not proof that the implementation is correct. Inspect assertion quality and missing cases.

Do not install dependencies, update lockfiles, start paid services, or use production credentials merely to complete the baseline. Mark blocked or unavailable checks accurately.

## Plan independent review lenses

Use independent reviewers when agent tooling is available and the repository is large enough to benefit. Give each reviewer a bounded surface and a distinct lens, and ask for candidate findings with code paths and reproduction evidence. Keep final verification and severity assignment with the coordinating reviewer.

Cover every applicable lens, in parallel where practical and sequentially otherwise:

1. Security: authentication, authorization, RBAC, tenancy, secrets, injection, data exposure, and abuse paths.
2. Language correctness: type and runtime behavior, async, cancellation, concurrency, lifecycle, resource ownership, and races.
3. Persistence: SQL, migrations, constraints, foreign keys, transactions, isolation, and database-specific behavior.
4. Business invariants: billing, credits, budgets, quotas, rate limits, accounting, idempotency, and observability.
5. Architecture: layer boundaries, dependency direction, duplicated policy, state ownership, and concrete maintainability risks.
6. APIs and integrations: validation, error mapping, timeouts, retries, streaming, provider adapters, and partial failure.
7. Frontend when present: credential handling, forms, pagination, state transitions, error rendering, and API/UI divergence.
8. Delivery and operations: test gaps, CI/CD, containers, dependency security, configuration drift, startup, shutdown, and recovery.
9. Adversarial cross-feature behavior: combine individually valid actions to seek escalation, bypass, double spending, stale state, or inconsistent transitions.

Adapt emphasis to the stack, but do not silently omit a relevant lens. Declare whether reviewers ran in parallel or the coordinator covered the lenses directly.

## Verify candidate findings

For every candidate considered for inclusion:

1. Open every involved function and enough surrounding code to understand the full behavior.
2. Trace the path end to end through UI or client, middleware, controller, dependencies, service, domain logic, repository, adapters, and database as applicable.
3. Inspect callers, callees, alternate entry points, configuration, migrations, and tests.
4. Search for compensating controls in other layers and verify whether they apply to the exact path.
5. Confirm third-party behavior from installed source or current official documentation when the conclusion depends on it.
6. Reproduce the issue when practical with a targeted test, minimal script, test client, SQL probe, concurrency schedule, cancellation simulation, or captured provider request.
7. Classify the evidence as:
   - **Confirmed:** reproduced or deterministically demonstrated on the current tree.
   - **Strongly supported inference:** the relevant behavior is directly verified, but an unavailable external condition prevents complete reproduction.
   - **Theoretical:** plausible but not demonstrated.
8. Include only confirmed findings and strongly supported inferences with a concrete, bounded impact. Exclude theoretical risks from Findings; record useful exclusions under Verified and refuted.
9. Capture at least one precise current-tree reference in `relative/path:line` or `relative/path:start-end` form. Re-check line numbers after all report edits.
10. Compare the candidate with the historical ledger and other candidates before assigning a new stable ID.

Explicitly test equivalent surfaces rather than extrapolating from one path. When present, compare streaming and non-streaming flows, pre-first-chunk and mid-stream failure, disconnect and cancellation, create/update/revoke/delete/recreate transitions, native and compatibility APIs, and UI versus direct API behavior.

## Assign severity

Base severity on demonstrated exploitability, impact, blast radius, prerequisites, and likelihood:

- **CRITICAL:** concrete privilege escalation or authentication bypass; exposure of highly sensitive secrets or data; severe systematic data manipulation; exploitable relay behavior; or significant direct economic bypass.
- **HIGH:** important security failure with prerequisites; materially incorrect billing or budgets; silently wrong successful results; systematic loss of accounting or observability; high-impact race; reproducible load failure; or ineffective administrative controls.
- **MEDIUM:** real but bounded correctness, consistency, transactional, operational, pagination, error-handling, or environment-drift defect.
- **LOW:** verified limited-impact defect, localized maintainability liability with a concrete failure mode, or targeted hardening gap. Do not use LOW for stylistic preferences.

Do not inflate severity to make the report look substantial or lower it to make the result reassuring.

## Write one report

Produce a single Markdown report with the following sections in this order. Substitute concrete values and omit no required section:

```markdown
# Code Review — Round N (scope)

[← Index](relative-index-path) <!-- Include only when an index exists. -->

Introduction

## Executive summary

Counts: **X CRITICAL · X HIGH · X MEDIUM · X LOW**.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|

## Findings

### ISSUE-NNN — Precise behavior and impact (severity)

**Where.**

**Problem.**

**Why it is a problem.**

**Verified impact.**

**Suggested fix.**

## Resolution status

## Deferred / product decision

## Verified clean

## Verified and refuted

## Category scores
```

In the introduction, state the resolved scope or commit range, relationship to previous rounds, reviewer count and lenses, verification standard, and baseline actually executed.

In the executive summary, first describe what withstood review and cite meaningful checks. Then identify the main theme of new defects without generic language. End with exact counts.

Order findings by severity and then impact. Continue historical stable IDs when present; otherwise start at `ISSUE-001`. Use `Open` for review-only findings. For every finding:

- name exact files, line ranges, functions, and components;
- describe current behavior and why existing defenses do not prevent it;
- identify the violated invariant or contract;
- state the reproduction command or procedure and observed result;
- distinguish direct verification from inference;
- propose the smallest architecture-compatible fix, correct layer, transaction semantics or migration when needed, expected behavior, and regression test.

Keep genuine product or architectural decisions out of Findings when no current contract is violated. Put them under Deferred / product decision with the relevant trade-off.

Under Verified clean, list only sensitive areas that were traced deeply enough to prevent repeated review work. Under Verified and refuted, record significant false positives, protections found in another layer, non-reproductions, by-design behavior, resolved historical findings, and corrected library interpretations.

Score each applicable category from 1 to 10 with one evidence-backed sentence: Security & tenancy, Correctness, Async & concurrency, Persistence & transactions, Billing / business invariants, Architecture & maintainability, Testing, Operations / production readiness, and Frontend. Mark truly inapplicable categories `N/A` instead of inventing a score. End with an overall score and a balanced assessment.

If there are no findings, keep the empty Issue summary and Findings sections and explain why. Do not manufacture issues to populate them.

## Remediate only when requested

When the user explicitly requests fixes as well as review:

1. Write a focused regression test that fails for the verified reason.
2. Run it and capture the failing behavior.
3. Implement the smallest correction at the appropriate layer without broad unrelated refactoring.
4. Run the targeted test, then the full relevant suite and all available gates.
5. Re-read the corrected end-to-end path against the original finding; a passing new test alone is insufficient.
6. Re-run security checks appropriate to the changed surface.
7. Update the report with verified resolution evidence and the current status. Mention a commit or pull request only if it actually exists.

Maintain immutability where the project conventions require it, validate input at system boundaries, parameterize database access, preserve authorization checks, and handle errors explicitly.

## Final quality gate

Before delivering the report:

- verify every issue ID, count, severity, status, and table entry is consistent;
- verify every cited path and line range exists in the final tree;
- confirm every claimed command appears in the execution record;
- confirm no historical duplicate is presented as new;
- confirm no secret value appears in the report;
- confirm findings describe defects rather than style preferences or speculative risk;
- confirm review-only mode changed no product source file;
- run a Markdown check when the repository provides one;
- inspect the final diff or generated report before handoff.
