# Code Review — Findings Index

Whole-codebase security & quality review of `src/litestar_gateway`, run in repeated rounds as
the codebase grows. Each round is a separate reviewer pass, one file per round below, always
**verified against the actual code** — every finding cites `file:line`, the concrete impact,
and a suggested fix, and is cross-checked against prior rounds so nothing already tracked is
re-reported. Severity reflects verified exploitability/impact, not the raw finder claim.

## Rounds

| Round | Focus | New findings (C·H·M·L) | Status |
|---|---|---|---|
| [Round 9](round-9.md) — 2026-07-14 | Full-tree post-R8 (admin UI, org/team CRUD, invites, RPM, API-key rotation) | 1·4·6·2 | C001 fixed; 4H+6M+2L open |
| [Round 8](round-8.md) — 2026-07-09 | Native provider-endpoint surface (Anthropic/Gemini passthrough, conformance, auth/error changes) | 1·3·3·3 | 1C+3H+3M+L008 fixed; L010 covered; L009 deferred |
| [Round 7](round-7.md) — 2026-07-08 | Fresh-eyes full-tree review (5 lenses: web/auth, concurrency, adapters, persistence, ops/tests) | 0·3·10·6 | 3H+10M fixed; 6L deferred |
| [Round 6](round-6.md) — 2026-07-08 | New-feature deep review (routing, SCIM, Bedrock, RBAC/SSO, maintainability) | 2·6·12·5 | Fully remediated |
| [Round 5](round-5.md) — 2026-07-06 | Graph-guided full-project review | 0·1·4·3 | Fully remediated |
| [Round 4](round-4.md) — 2026-07-06 | Full-project review (money/concurrency edges) | 0·1·7·8 | Remediated except L25 (deferred) |
| [Round 3](round-3.md) — 2026-07-03 | Money & concurrency review | 0·1·5·8 | Remediated except L15 (deferred) |
| [Round 2](round-2.md) — 2026-07-02 | Enterprise-readiness review | 0·7·7·4 | Remediated except M18–M20, L7 (deferred) |
| [Round 1](round-1.md) — initial review | Initial full-project review | 1·5·13·6 | Remediated except M7, M11, M13, L1–L6 (deferred); H1 (credentials) reclassified by design |

## Overall

**As of Round 9: 6.8/10** — see [round-9.md](round-9.md#category-scores-this-round) for the
category breakdown. The historical auth/tenancy core remains intact; the RBAC escalation in
the generic API-key rotate endpoint is fixed by #249, while the two regressions in personal-key
revocation/ownership remain open and dominate the current score. The remaining post-R8 work
covers rate-limit coverage, invite/FK lifecycle, and admin-UI session/pagination correctness.
Each round's own deferred items remain listed in that round's "Resolution status" section.
