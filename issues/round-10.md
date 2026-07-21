# Code Review — Round 10 (2026-07-21)

[← Index](INDEX.md)

Tenth full pass, over the changes after Round 9 (range `d4c2871..d1e23d4`,
PRs 248–280), re-checked against the current tree. The delta covers: the Round 9
remediations (#249–#259), the console pages for service principals / credentials
/ models / routing / observability, the role-aware dashboard with its new
endpoints (`GET /me/teams`, `GET /teams/{id}/savings`, `GET /routing/savings`,
`service_principal_id` on `KeyResponse`), the docs refresh, and quiet-404 logging.

Four independent passes (security, Python correctness, TypeScript/UI, adversarial
cross-feature); every finding reported here was re-verified against the real code
with `file:line` citations. The suite and static gates are green (872 passed;
Ruff, Pyrefly, pre-commit, ESLint, tsc and the Vite build all clean).

Counts: **0 CRITICAL · 2 HIGH · 4 MEDIUM · 3 LOW**.

## Executive summary

**No CRITICAL finding, and — for the first time — no HIGH security finding.**
The Round 9 remediations hold under a dedicated adversarial pass: the webhook
bearer-token mask/envelope is correct across the whole PUT round-trip, the
invite↔team lifecycle is mutually exclusive via a lock, rotation restores the
owner/SP and refuses inactive keys, per-key RPM covers every inference surface,
`/me/teams` is self-scoped, and savings are correctly gated.

The most important finding is a **data-modelling** one: `routing_decision` rows
are keyed by `(team_id, router_name)` with no `router_id`. A deleted router
keeps inflating team and platform savings/stats forever and — worse — **reusing
the name** of a deleted router contaminates the new router with the old one's
history, including the JSONL distillation export that carries raw user prompts,
across two logically distinct configurations.

On the UI side the recurring theme is **fabricating empty/zero states on error**:
the dashboard shows "$0.00" spend when the organizations fetch fails, Budgets
answers "unlimited spend" on a fetch error, and stats/savings/audit render a
"—"/empty that's indistinguishable from a 403 or 500.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-001 | Routing decisions keyed by name: deleted routers pollute savings and a reused name contaminates the history (export included) | high | `persistence/orm.py:212-223`; `domain/ports/routing.py:41-59`; `application/routing/service.py:586-640`; `persistence/router_repository.py:279-288` | **Fixed** (#282) |
| ISSUE-002 | The dashboard shows "$0.00 · no spend recorded" when the organizations fetch fails | high | `ui/src/features/dashboard/DashboardPage.tsx:117-152` | **Fixed** (#283) |
| ISSUE-003 | `GET /me/teams` silently truncates at 100 memberships and does N+1 lookups | medium | `application/team_service.py:284-293`; `web/session/me.py:31-39` | **Fixed** (#284) |
| ISSUE-004 | Budgets / Audit / Router-detail render errors as empty states ("unlimited", "no events", "—") | medium | `ui/src/features/budgets/BudgetsPage.tsx:117-140`; `ui/src/features/dashboard/DashboardPage.tsx:105-108,218-221`; `ui/src/features/routing/RouterDetailPage.tsx:135-142,183-198` | **Fixed** (#283) |
| ISSUE-005 | An explicit `0` in cost/threshold fields is dropped without feedback (`parsePositive`) | medium | `ui/src/features/models/CreateModelDialog.tsx:35-40,252-259`; `ui/src/features/routing/CreateRouterDialog.tsx:67-72,558-568` | **Fixed** (#283) |
| ISSUE-006 | Race `delete_user`↔`add_member`: the FK violation is relabelled `AlreadyMember` (misleading 409) | medium | `application/user_service.py:211-224`; `persistence/membership_repository.py:21-39` | **Fixed** (#284) |
| ISSUE-007 | `_savings_aggregate` runs 3 non-atomic SELECTs for a single figure | low | `persistence/router_repository.py:290-323` | **Fixed** (#282) |
| ISSUE-008 | `delete_user`'s guard is a manual FK list: a future FK to `user_account` would 500 | low | `application/user_service.py:201-225` | **Fixed** (#284) |
| ISSUE-009 | Widespread `error as Error \| null` casts on useQuery errors | low | ~15 call sites in `ui/src/features/*` | **Fixed** (#283) |

## Findings

### ISSUE-001 — Routing decisions keyed by name (high)

`RoutingDecisionModel` has only `team_id` + `router_name` — no `router_id`
column, no FK to `router` (`persistence/orm.py:212-223`). The router delete is
hard and doesn't touch the decisions (`persistence/router_repository.py:171-178`);
the `UniqueConstraint("team_id", "name")` (`orm.py:169-171`) holds only among
*existing* routers, so the name is freed immediately. Every read —
`list_decisions`, `distribution`, `savings`, the JSONL export — filters by
`(team_id, router_name)` (`domain/ports/routing.py:41-59`,
`application/routing/service.py:586-640`); `team_savings`/`platform_savings`
aggregate every row with no join to `router`
(`persistence/router_repository.py:279-288`).

Reproduction: (a) create "prod-router", generate traffic, delete it → its numbers
stay in `GET /routing/savings` and `GET /teams/{id}/savings` forever, not
excluded and labelled in the dashboard only as "all time"; (b) recreate a router
with the same name and different strategy/candidates → the *new* router's
`decisions`, `stats`, `savings` and `/decisions/export` show the old one's
history mixed in. The distillation export carries raw user prompts: the
misattribution across two distinct configurations is a data-hygiene problem, not
just a metrics one.

Suggested fix: add `router_id: UUID` (nullable, no FK cascade — history must
survive the delete on purpose) to `routing_decision`, populate it on write, and
filter the per-router endpoints by id. If team/platform totals should include
deleted routers that's a legitimate product choice, but it must be stated (UI/docs
copy); endpoints scoped to *one* router must never show another's history just
because they shared a name.

### ISSUE-002 — Dashboard: "$0.00" on an org-fetch error (high)

`DashboardPage.tsx:117-152`: if the `["organizations","all"]` query fails
(`retry:false`), `spendQueries` becomes an empty array; the render guard
`spendLoaded || spendQueries.length === 0` is true and shows `formatUsd(0)` =
**"$0.00"**, with "no spend recorded yet." below it. On the same screen the
"organizations" card correctly shows "—" for the same error: the page
contradicts itself, and a financial view reports a false zero instead of an
error state. Fix: an explicit branch on `orgs.isError` (and `orgs.isLoading`)
for the spend panel.

### ISSUE-003 — `/me/teams` truncated at 100 + N+1 (medium)

`TeamService.list_user_teams` (`team_service.py:284-293`) calls
`memberships.list_by_user` with no `limit` → it falls back to the default
`DEFAULT_PAGE_SIZE` (100): a user in 101+ teams never sees the memberships past
the hundredth, with no truncation signal (the endpoint has no pagination
params). It also does one `teams.get()` per membership (N+1, bounded at 100).
Fix: batch fetch (`WHERE id IN`), plus explicit pagination or iteration to
exhaustion, since the contract is "all my teams".

### ISSUE-004 — Errors rendered as empty states in Budgets/Audit/Router detail (medium)

Same pattern across three surfaces: `BudgetsPage.tsx:117-140` never consults
`budget.isError` and on a fetch error shows the "no budget configured — this
team's spend is unlimited" copy (the API layer correctly distinguishes 404 from
an error after the R9 ISSUE-013 fix, but the page doesn't consume the signal);
`DashboardPage.tsx:218-221` renders an audit-query error as "no audit events
yet."; `RouterDetailPage.tsx:135-142,183-198` renders stats/savings as "—",
identical for loading, 403 and 500 (while the decisions table below correctly
passes `error` to the DataTable). Fix: an `isError` branch with a message, as the
list pages already do.

### ISSUE-005 — Explicit `0` dropped by the forms (medium)

`parsePositive` requires `n > 0`: in the model cost fields
(`CreateModelDialog.tsx:252-259`, inputs with `min="0"`) a legitimate `0`
("this model is free") becomes `null` = "unset" with no feedback; in the
embeddings route threshold (`CreateRouterDialog.tsx:558-568`, `min="0" max="1"`)
the backend requires `0 < t <= 1` and defaults to `0.80`, so typing `0` silently
yields a 0.80 threshold. Fix: inline validation (a visible error for
out-of-contract values) and `min` attributes aligned to the real range; accept
`0` where the backend allows it.

### ISSUE-006 — delete_user ↔ add_member race relabelled (medium)

`delete_user` takes `FOR UPDATE` on the user, checks for no memberships/keys and
deletes (`user_service.py:211-224`). A concurrent `add_member` (pre-check +
INSERT, `membership_repository.py:21-39`) that loses the race fails with an
`IntegrityError` on the missing parent FK — but the adapter catches *any*
`IntegrityError` and unconditionally raises `AlreadyMember`: the admin gets a
409 "already a member" for a just-deleted user. Fix: distinguish the cause (FK
violation vs unique) or re-check the user's existence before relabelling;
deserves a targeted test, in line with the races closed in R9.

### ISSUE-007 — Non-point-in-time savings aggregate (low)

`_savings_aggregate` (`router_repository.py:290-323`) runs three separate SELECTs
(SUM, counted, all) with no snapshot: under live traffic `decisions_without_usage`
can come out inconsistent (negative in an adversarial case, it isn't clamped).
Reporting only, not billing. Fix: collapse into one query
(`COUNT(*) FILTER (WHERE …)` + a conditional SUM) — it's both correct and 1 round
trip instead of 3.

### ISSUE-008 — delete_user guard as a manual list (low)

The guard checks only memberships and `api_key.created_by`; today no other FK to
`user_account` is unhandled (verified: `password_reset` is cleaned in the delete,
`audit.actor_id` isn't an FK), so it's **not a live bug** — but a future FK added
without updating the guard would produce a generic 500 (correct rollback, no
corruption). A maintenance trap: comment the guard with the invariant, or derive
the check from the FKs at runtime in a test.

### ISSUE-009 — `as Error | null` casts (low)

~15 call sites cast `useQuery().error` to `Error | null`. Today every `queryFn`
constructs a real `new Error(...)`, so it's a safe no-op; but a future non-Error
rejection would be silently mis-typed. Low-cost fix: `instanceof Error` narrowing
in a shared helper.

## Resolution status — FULLY REMEDIATED

All nine findings are fixed across three PRs:

- **#282** — ISSUE-001 (routing decisions keyed by `router_id`, not name;
  deleted/reused-name history no longer leaks) and ISSUE-007 (savings aggregate
  collapsed to one point-in-time query).
- **#283** — ISSUE-002 (the dashboard no longer fabricates "$0.00" on load
  failure), ISSUE-004 (Budgets/audit/router-detail surface errors distinctly),
  ISSUE-005 (explicit `0` costs accepted; embeddings threshold validated
  in-form), ISSUE-009 (a shared `toError` helper replaces the `as Error | null`
  casts).
- **#284** — ISSUE-003 (`/me/teams` complete, no 100-cap, batched lookup),
  ISSUE-006 (`add_member` reports the real cause on a concurrent-deletion race),
  ISSUE-008 (a schema-invariant test guards the `user_account` FK set used by
  `delete_user`).

## Verified and refuted (for the next round)

- **Router webhook enable/disable round-trip**: the `***` mask echoed on PUT is
  restored from the encrypted envelope in `_preserve_masked_tokens`
  (`router_repository.py:73-99,149-169`), including the `shadow` section; the
  response candidate dicts match `CandidateRequest` 1:1. No loss/corruption.
- **Invite → team deleted → redeem**: `register()` and `delete_team()` serialize
  on `lock_for_lifecycle`; `register` checks the team exists *before* burning the
  invite. The #253 fix holds.
- **Rotate + `service_principal_id`**: rotation re-checks the owner/SP under a
  lock and propagates `service_principal_id`; an inactive key can't be rotated.
- **Budget window change mid-window**: `window_start` is recomputed statelessly
  on each read — no persisted counter to invalidate.
- **Invite-token transport post-#254**: URL fragment only, captured into an
  in-memory store and scrubbed with `history.replaceState` before the router
  observes the location; the path check is consistent with the `/ui` basepath.
- **Italian locale in numeric fields**: `<input type="number">` always
  normalizes the separator — a comma isn't a vector.
- **Note (not an issue)**: `provide_principal` now also authenticates via the
  session cookie; the path is protected (SameSite=Strict + CSRF on mutations),
  but `Principal`'s trust boundary now includes a human via cookie — worth
  keeping in mind when adding new `provide_principal`-gated endpoints.

## Category scores (this round)

| Category | Score | Summary |
|---|---:|---|
| Security / RBAC | **8.5/10** | Zero findings: the R9 remediations hold under a dedicated adversarial pass; new-endpoint authz correct end-to-end. |
| Money / rate limiting | **8/10** | Full RPM coverage; savings are reporting-only, but the name-keying undermines their trustworthiness (ISSUE-001). |
| Persistence / lifecycle | **7.5/10** | UoW and locks well used; the decision name-keying and a labelling race remain (ISSUE-006). |
| Admin UI | **7/10** | Typed, paginated, token hygiene in place; the open theme is rendering errors as empty/zero states (ISSUE-002/004/005). |
| Test / CI | **8.5/10** | 872 green and strong gates; missing tests for UI error states and router-name reuse. |

**Overall: 7.8/10.** A clear jump from Round 9's 6.8: the security/tenancy core
produced no new findings and the earlier fixes are confirmed by adversarial
verification. The remaining work is product robustness: a `router_id` on the
decisions and honest error states in the console.
