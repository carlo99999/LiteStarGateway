# Code Review — Findings Index

Whole-codebase security & quality review of `src/litestar_gateway`, run in repeated rounds as
the codebase grows. Each round is a separate reviewer pass, one file per round below, always
**verified against the actual code** — every finding cites `file:line`, the concrete impact,
and a suggested fix, and is cross-checked against prior rounds so nothing already tracked is
re-reported. Severity reflects verified exploitability/impact, not the raw finder claim.

## Rounds

| Round | Focus | New findings (C·H·M·L) | Status |
|---|---|---|---|
| [Round 15](round-15.md) — 2026-07-29 | Delta dopo `5469464`: provider openai_compatible + egress allowlist, guardrail (chain/webhook/judge/policy/router-scope), prenotazioni budget distribuite + Decimal/NUMERIC, budget per-chiave, segreto webhook per-team, capability dichiarate, reliability view | 0·7·6·6 | Fully remediated (#442–#453) |
| [Round 14](round-14.md) — 2026-07-28 | Regression review of the Round 13 remediation (#392–#401): pricing, cache equivalence, client lifecycle, outbox, deadline, SSO, breaker, purge | 0·0·3·0 | Fully remediated (#403–#405) |
| [Round 13](round-13.md) — 2026-07-27 | Post-R12 delta (response cache exact+semantic, failover/breaker, budget alerts, non-token pricing, client registry, DB-backed SSO, retention/purge) | 0·2·7·0 | Fully remediated (#392–#401) |
| [Round 12](round-12.md) — 2026-07-22 | Post-R11 delta (verification of remediation PRs #313–#321: alias registry, router revisions, playground governance, downgrade contract) | 0·0·2·0 | Fully remediated |
| [Round 11](round-11.md) — 2026-07-22 | Post-R10 delta (global/extended routers & models, Playground, credential lifecycle, reasoning params, admin console) | 0·4·6·0 | Fully remediated |
| [Round 10](round-10.md) — 2026-07-21 | Post-R9 delta (console completa, savings/me-teams endpoints, R9-fix verification) | 0·2·4·3 | Fully remediated |
| [Round 9](round-9.md) — 2026-07-14 | Full-tree post-R8 (admin UI, org/team CRUD, invites, RPM, API-key rotation) | 1·4·6·2 | Fully remediated |
| [Round 8](round-8.md) — 2026-07-09 | Native provider-endpoint surface (Anthropic/Gemini passthrough, conformance, auth/error changes) | 1·3·3·3 | 1C+3H+3M+L008 fixed; L010 covered; L009 deferred |
| [Round 7](round-7.md) — 2026-07-08 | Fresh-eyes full-tree review (5 lenses: web/auth, concurrency, adapters, persistence, ops/tests) | 0·3·10·6 | 3H+10M fixed; 6L deferred |
| [Round 6](round-6.md) — 2026-07-08 | New-feature deep review (routing, SCIM, Bedrock, RBAC/SSO, maintainability) | 2·6·12·5 | Fully remediated |
| [Round 5](round-5.md) — 2026-07-06 | Graph-guided full-project review | 0·1·4·3 | Fully remediated |
| [Round 4](round-4.md) — 2026-07-06 | Full-project review (money/concurrency edges) | 0·1·7·8 | Remediated except L25 (deferred) |
| [Round 3](round-3.md) — 2026-07-03 | Money & concurrency review | 0·1·5·8 | Remediated except L15 (deferred) |
| [Round 2](round-2.md) — 2026-07-02 | Enterprise-readiness review | 0·7·7·4 | Remediated except M18–M20, L7 (deferred) |
| [Round 1](round-1.md) — initial review | Initial full-project review | 1·5·13·6 | Remediated except M7, M11, M13, L1–L6 (deferred); H1 (credentials) reclassified by design |

## Overall

**As of Round 15: 6.8/10 at review time, all nineteen findings since remediated
(#442–#453).** See
[round-15.md](round-15.md#category-scores). Il delta aggiunge il provider
openai_compatible con egress allowlist, i guardrail (con scope per-router), le
prenotazioni di budget distribuite, il denaro Decimal/NUMERIC e i budget
per-chiave; la base storica regge (primitive di egress byte-for-byte, firma HMAC
corretta, atomicità Redis in un solo Lua, decimalizzazione a una regola). Il
tema dei sette HIGH è però unico e ricorrente su tre feature: **un controllo
dichiarato non viene richiamato sul percorso alternativo** — l'allowlist egress
non è applicata al dispatch (034), la redazione è annullata sul failover (035),
la response cache serve il body pre-guardrail (036), il budget per-chiave è
no-op sulla superficie OpenAI (037), gli endpoint native saltano il guard di
richiesta (038), due redattori non compongono (039) e il purge del team omette
le regole guardrail team-wide, bloccandolo (040). Nessuna escalation
cross-tenant. Un caso è REFUTED: il presunto `SyntaxError` bloccante è codice
valido su Python 3.14 (PEP 758) e la suite passa (2184 test).

Previous: **as of Round 14: 8.6/10 at review time, all three findings since remediated
(#403–#405).** See [round-14.md](round-14.md#category-scores) for the breakdown.
The round verified the nine Round 13 remediations hold and found three MEDIUMs
in their transition edges — a no-channel alert holding its dispatch lease for
the full 300 s (#403), a DB SSO configuration created before #398 still
reopening the `Host` fallback (#404), and a breaker outcome able to resolve a
half-open trial it never earned (#405). Two of the three were introduced or left
incomplete by the Round 13 remediation itself, which is the argument for running
a regression round after a large remediation rather than declaring it closed.
Their shared lesson: a concurrent protocol needs an *identity*, not just a
state — the fix in all three cases was making the owner explicit.

Previous: **as of Round 13: 7.4/10 at review time, all nine findings since remediated
(#392–#401).** See [round-13.md](round-13.md#category-scores) for the category
breakdown. The two HIGHs were economic/result correctness: a negative model rate
credited the ledger the budget gate reads (#392), and both cache tiers could
serve a response produced for a different model, operation or policy
(#393/#394). The seven MEDIUMs were incomplete concurrent protocols (alert
outbox atomicity and per-replica duplication, client-registry generation loss,
Redis half-open) and declared-but-unenforced constraints (failover deadline,
DB-backed SSO redirect, team purge completeness, semantic-cache global bound).
Each fix landed with a regression that fails without it; two recurring test
weaknesses surfaced along the way — fakes that ignore TTLs, and single-session
stand-ins for multi-replica races. Re-verification belongs to Round 14.

Previous: **as of Round 12: 9.1/10** — see [round-12.md](round-12.md#category-scores) for the category
breakdown. Round 12 re-verified the entire Round 11 remediation (PRs #313–#321) against the
current tree with six independent lenses plus an adversarial cross-feature pass: **zero new
security findings**, all ten Round 11 issues hold, and all ten probed cross-feature combinations
(team/global homonyms, alias tombstone/reclaim, post-grant edits, credential rotation in flight,
playground fan-out, deletions with historical usage, webhook re-approval, candidate identity,
promotions) verified SAFE. The two MEDIUMs were non-uniform tails of the remediation itself and
are now **fixed and merged**: deleting a granted model silently cascade-revoked other teams'
access where routers raise `RouterShared` (ISSUE-020, fixed by #332), and the Usage/Budgets
console surfaces were unreachable for `billing-viewer` and platform auditors — the exact roles
the backend authorizes (ISSUE-021, fixed by #333). No findings remain open.

Previous: as of Round 10, 7.8/10 — see [round-10.md](round-10.md#category-scores-this-round) for the
category breakdown. Round 10 produced **zero security findings**: a dedicated adversarial pass
confirmed the Round-9 remediations hold end-to-end (webhook token masking round-trip, invite/team
lifecycle locks, rotation ownership, per-key RPM coverage, self-scoped /me/teams). The open work
is product robustness: routing decisions are keyed by router *name* instead of id (deleted-router
history pollutes savings and a reused name cross-contaminates a new router, ISSUE-001), and the
console renders several error states as empty/zero states (ISSUE-002/004/005).

Previous: as of Round 9, 6.8/10 — see [round-9.md](round-9.md#category-scores-this-round) for that
breakdown. The historical auth/tenancy core remains intact; the RBAC escalation in
the generic API-key rotate endpoint is fixed by #249; personal-key ownership, immediate
revocation and atomic rotation by #250; inference per-key RPM coverage by #251; and last-admin
lifecycle races by #252; invite/team FK lifecycle by #253; and invite-token transport by
PR #254; and the admin UI session is protected by an HttpOnly cookie plus CSRF in
PR #255; budget errors remain distinguishable from absence in PR #257; and inactive
rate-limit buckets are pruned by PR #258; and admin UI collections are fully
paginated by PR #259. All Round 9 findings are remediated.
Each round's own deferred items remain listed in that round's "Resolution status" section.
