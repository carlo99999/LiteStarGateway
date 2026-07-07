# Code Review — Findings Index

Whole-codebase security & quality review of `src/litestar_gateway`, run in repeated rounds as
the codebase grows. Each round is a separate reviewer pass, one file per round below, always
**verified against the actual code** — every finding cites `file:line`, the concrete impact,
and a suggested fix, and is cross-checked against prior rounds so nothing already tracked is
re-reported. Severity reflects verified exploitability/impact, not the raw finder claim.

## Rounds

| Round | Focus | New findings (C·H·M·L) | Status |
|---|---|---|---|
| [Round 6](round-6.md) — 2026-07-08 | New-feature deep review (routing, SCIM, Bedrock, RBAC/SSO, maintainability) | 2·6·12·5 | Open |
| [Round 5](round-5.md) — 2026-07-06 | Graph-guided full-project review | 0·1·4·3 | Fully remediated |
| [Round 4](round-4.md) — 2026-07-06 | Full-project review (money/concurrency edges) | 0·1·7·8 | Remediated except L25 (deferred) |
| [Round 3](round-3.md) — 2026-07-03 | Money & concurrency review | 0·1·5·8 | Remediated except L15 (deferred) |
| [Round 2](round-2.md) — 2026-07-02 | Enterprise-readiness review | 0·7·7·4 | Remediated except M18–M20, L7 (deferred) |
| [Round 1](round-1.md) — initial review | Initial full-project review | 1·5·13·6 | Remediated except M7, M11, M13, L1–L6 (deferred); H1 (credentials) reclassified by design |

## Overall

**As of Round 6: 6.5/10** — see [round-6.md](round-6.md#category-scores-this-round) for the
category breakdown. The architecture and money/security core (parameterized queries, pinned
JWT alg, tenant scoping, no secret leakage in response DTOs) have held up across all six
rounds; the open work is concentrated in Round 6's governance/data-exposure findings (C2, C3,
and the six HIGHs) plus each round's own deferred items (larger refactors or product
decisions, listed in that round's "Resolution status" section).
