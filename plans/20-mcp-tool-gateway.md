# Plan 20 — MCP tool gateway

**Design doc:** [docs/next-steps/mcp-tool-gateway.md](../docs/next-steps/mcp-tool-gateway.md).
**Depends on:** nothing unshipped. It reuses the egress primitives
(`domain/egress_policy.py`, `application/egress.py`,
`post_to_approved_address`), the three-origin visibility machinery
(`CallableOrigin`, `CallableAliasResolver`), the guardrail chain, the keyring's
envelope encryption, `UsageMeter` and the audit trail.
**Theme:** the gateway governs what a model call *says* and nothing it *does*.
This brings tool execution inside the perimeter — egress, audit, spend — without
becoming an agent framework.

Ground rules (unchanged from Plans 17–19): one reviewable PR per slice; every PR
carries a regression that **fails before the fix**; full gate before each
(`uv run pytest -q --cov-fail-under=80`, `ruff check`, `ruff format --check`,
`pyrefly check`, `just ui-ci`), plus `just test-postgres` and
`just migration-check` for anything touching Alembic.

One ground rule is **new**, and it is the Round 15 lesson written as a process
rule rather than a hope:

> **Every control this plan adds must be tested on each path that can bypass it.**
> Round 15's seven HIGHs were one defect repeated: a declared control not
> re-applied on the alternative path — the failover retry, the cache replay, the
> native endpoint, the stream, the dispatch. This feature adds a control (tool
> authorization) and a new path (the tool loop), so the same trap is pre-built. A
> slice that adds a check without a test for the loop, the failover retry, the
> cache and the stream is not done.

## Decisions taken up front

Settled here so no slice re-opens them. Each is argued in the design doc; this is
the executable summary.

- **D1 — HTTP transport only.** No stdio: it would mean spawning per-tenant
  subprocesses in a multi-tenant control plane. An operator wanting a stdio
  server runs it behind an HTTP shim and allowlists it.
- **D2 — a server is a team resource; the platform keeps one veto.** Team admins
  create and remove servers; `MCP_ALLOWED_HOSTS` bounds where any of them may
  point, checked on write **and re-resolved per call**.
- **D3 — effects are declared, never detected.** `read` | `write` |
  `destructive`, set by the operator, optionally seeded from MCP annotations but
  never trusted from them. An unclassified tool counts as `destructive`.
- **D4 — keys are permissive except for destructive.** Absent per-key policy
  means unrestricted (the polarity a missing spend cap already has); a
  `destructive` tool needs explicit per-key enablement, default off. The request
  contract is untouched — no client learns a new field.
- **D5 — visibility resolves in one place.** Never `server.team_id == team_id`:
  that spelling excludes globals and extended servers, and it has bitten this
  codebase twice (guardrail scope on global models, Round 12's ISSUE-020).
  Resolution goes through `CallableAliasResolver`/`CallableOrigin`.
- **D6 — no execution by default.** `"tool_execution": "gateway"` opts in; absent
  is byte-identical to today.
- **D7 — one Alembic head in flight.** Two slices need migrations (S0, S5); they
  are never open at the same time.
- **D8 — OpenAPI artifacts are regenerated once, at integration.** Four slices
  change request or response schemas. Regenerating `ui/openapi.json` +
  `ui/src/lib/api/schema.ts` per branch guarantees a textual conflict on every
  merge, and `just ui-schema-check` is only meaningful against an integrated
  tree — measured the hard way while shipping Round 15.

## Why this order

The dependency graph is shallow, so the real constraint is file ownership, same
as Plan 17. Two files are the bottleneck: **`completion_service.py`** (S6, S7)
and **`usage_meter.py`** (S7). Everything before them is new files and can run
wide; everything in them is one owner, strictly serial.

That asymmetry is the whole scheduling story: **Phase 0 is almost perfectly
parallel, Phase 2 is irreducibly serial.** A fan-out that ignores it produces
conflicting hunks on the same two files rather than throughput.

### Collision map

| File / area | Slices |
|---|---|
| `application/completion_service.py` | S6, S7 — **serial, one owner** |
| `application/usage_meter.py` | S7 |
| `domain/guardrails.py`, `application/guardrails/*` | S8 |
| `domain/mcp.py`, `persistence/mcp_*` (new) | S0, S1 |
| `web/mcp/*`, `web/mcp_platform/*` (new) | S2, S5 |
| `infrastructure/mcp/client.py` (new) | S3, S7 |
| `migrations/` | S0, S5 — **never concurrent** (D7) |
| `ui/`, `ui/openapi.json`, `schema.ts` | S2, S4, S5, S8 — regen at integration (D8) |
| `config.py` (additive) | S0 |

### Tracks

- **Track A — wide, any owner:** S0 → {S1, S3} → {S2, S4} → S5.
- **Track B — hot path, strictly serial, one owner:** S6 → S7.
- **Track C — gated:** S8 after S7 (it needs the loop to have something to guard).

Track A and Track B are disjoint until S6, so they can run at the same time.

---

## S0 — Domain, persistence, config (1 PR, ~1 d)

**Deliverables.** `domain/mcp.py`: `McpServer`, `McpTool`, `ToolEffect`
(`read`/`write`/`destructive`), reusing `CallableOrigin` rather than a new enum.
ORM + one migration: `mcp_server`, `mcp_server_grant` (mirroring
`model_grant`), `mcp_server_suppression` (the per-team detach of a global),
`mcp_tool` (discovered inventory + declared effect), `api_key_tool_policy`
(per-key allowlist + `destructive_enabled`). `config.py`:
`mcp_allowed_hosts: tuple[str, ...] = ()` from `MCP_ALLOWED_HOSTS`, parsed by the
**existing** `parse_allowlist` so a malformed entry fails at
`Settings.__post_init__` exactly like the openai_compatible one.

**Done when:** the migration applies and reverts on SQLite and PostgreSQL;
`just migration-check` is green; a table-driven test pins that an empty
`MCP_ALLOWED_HOSTS` permits nothing and a malformed entry refuses startup. FK
`ondelete` is explicit on every child — and `mcp_server.team_id` is in the team
purge child list from the start, because ISSUE-040 was exactly that table being
forgotten.

**Risk:** the suppression row is easy to model as a boolean on the grant. It is
not: a global server has no grant. Keep it a separate table keyed
`(team_id, server_id)`.

## S1 — Repository, service, visibility resolution (1 PR, ~1,5 d)

**Deliverables.** `McpServerRepository` port + SQLAlchemy adapter; envelope
encryption of `auth` on the `guardrail_rule.encrypted_secret` + `key_id` pattern,
with `has_auth` as the only thing any read exposes. `McpServerService`: CRUD,
allowlist validation on write, delete-vs-detach by origin, and **one**
`visible_servers(team_id)` that unions own + extended + global minus suppressed
(D5). Staged writes, service commits — and a `stage_set`-style method wherever a
caller owns the transaction, the lesson from Round 15's S12.

**Done when:** a global server is visible to a team that has no grant; detaching
it hides it from that team only and leaves it live for others; re-attaching
restores it; deleting a global as a team admin is impossible; the secret appears
in no response body or log line.

**Risk:** the temptation to write the visibility union inline in three places.
One function, and the tests call *it*, not the repository.

## S2 — REST surfaces (1 PR, ~1 d)

**Deliverables.** Team controller (`/teams/{id}/mcp-servers`, CRUD + detach) and
platform controller (`/platform/mcp-servers`, `/{id}/make-global`, `/{id}/extend`,
`GET /{id}/grants`, `DELETE /grants/{id}`) mirroring
`web/models/platform_controller.py`. `Permission.TOOLS_READ` /
`TOOLS_MANAGE` / `TOOLS_PROPOSE` added to `authorization.py`, with `TOOLS_PROPOSE`
in **every** role set (`ROLE_PERMISSIONS` does not inherit) and `TOOLS_MANAGE`
withheld from `model_manager`. Per-key policy endpoints under
`tools:read`/`tools:manage`, **not** `keys:issue` (ISSUE-042's asymmetry).

**Done when:** the RBAC matrix in the design doc §2.3 is a parametrized test —
every role × every endpoint, including the auditor seeing inventory but not the
call feed; a team admin of team A gets 404, not 403, on team B's server.

## S3 — Discovery client (1 PR, ~1 d)

**Deliverables.** `infrastructure/mcp/client.py`: `tools/list` over HTTP through
`post_to_approved_address` (pinned IP, Host/SNI kept, redirects off), bounded
timeout, strict response parsing. Cached inventory with a TTL and an explicit
refresh. Effects seeded from annotations, defaulted to `destructive` when absent
(D3).

**Done when:** a server whose DNS moves out of `MCP_ALLOWED_HOSTS` between two
discoveries is refused on the second — the ISSUE-034 regression shape, written
for this surface from day one rather than found by a review; a malformed
`tools/list` is a typed error, never a partially-populated inventory.

## S4 — Console: inventory and policy (1 PR, ~1 d)

**Deliverables.** A **Tools** page: servers with origin badges (own / extended /
global), inventory with effects, health and last discovery; per-key policy
editor; read-only call feed placeholder (populated in S7).

**Done when:** a failed inventory query renders an **error**, not an empty
inventory — the console defect this project has found in three separate rounds;
the `destructive` toggle is visibly off by default and its label says what
enabling it permits.

## S5 — Proposals (1 PR, ~1 d)

**Deliverables.** `mcp_server_proposal` (`pending → approved | rejected`) with the
second migration (D7), the atomic single-approval `UPDATE ... WHERE status =
'pending'` on the invite pattern, re-validation of the allowlist **at approval**,
discovery deferred to approval, rejection reason, audit on both outcomes, console
queue.

**Done when:** two concurrent approvals create one server; a proposal whose host
left the allowlist after filing is refused at approval; filing a proposal makes
**no** outbound request (asserted by a resolver double that fails the test if
called).

## S6 — Declaration injection (1 PR, ~1 d) · Track B

**Deliverables.** `tools: [{"type": "mcp", "server": …}]` expanded in `_prepare`
from the cached inventory, before the guardrail hook and before `_meter.admit`,
then handed to the existing `validate_chat_request` so `chat_tool_policy`'s
limits apply unchanged. Duplicate tool names are a 400.

**Done when:** the expanded declarations are what the guardrail chain sees and
what the provider receives; a request without an `mcp` reference is
byte-identical to today; a server the team cannot see is a 404 at expansion, not
a leak of its existence.

## S7 — Bounded execution (2 PRs, ~3 d) · Track B, one owner

**PR 1 — the loop.** Round loop with `max_rounds` (default 3, ceiling 10);
per-round `admit`/settle on the `guardrail.judge` precedent
(`OPERATION = "tool.call"`); grant + effect re-check at call time (the confused
deputy); per-call egress re-resolution; result byte cap with a truncation marker;
deadline inherited via `_within_deadline`.

**PR 2 — the alternative paths**, which is where the new ground rule bites:
buffered-then-streaming behaviour, interaction with the response cache, and with
failover. Tests required, not optional: a tool loop under failover screens with
the candidate that serves it; a cached response is not replayed past a tool
policy; a stream with gateway execution behaves per the documented rule.

**Done when:** every bullet above has a test that fails without it, and a
`tool.call` row carries server, tool, effect, latency and outcome — never
arguments or results.

**Risk:** this is the slice where Round 15's three traps have analogues. Watch
for: double-counting a rate limit by threading an id into an `admit` that already
gated it (ISSUE-037's shape); rebuilding a retry from a pre-guardrail body
(ISSUE-035's shape); a `finally` that turns a settled success into a 500
(ISSUE-045's shape).

## S8 — Guardrails on tools (1 PR, ~1,5 d) · Track C

**Deliverables.** `Direction.TOOL_CALL` and `Direction.TOOL_RESULT`; payload
extraction/re-insertion for both; `resolve_chain` and the fail policies unchanged;
an injection-focused judge prompt variant; console surface and audit.

**Done when:** a `TOOL_CALL` block is a tool error the model can react to, not a
422 to the client; a `TOOL_RESULT` redaction rewrites what reaches the model; the
chain stays sequential (the composition property from ISSUE-039).

---

## Sequence

| Slice | Track | Migration | PRs | Estimate |
|---|---|---|---|---|
| S0 domain/persistence/config | A | **yes** | 1 | 1 d |
| S1 repository/service/visibility | A | no | 1 | 1,5 d |
| S2 REST + RBAC | A | no | 1 | 1 d |
| S3 discovery client | A | no | 1 | 1 d |
| S4 console | A | no | 1 | 1 d |
| S5 proposals | A | **yes** | 1 | 1 d |
| S6 declaration injection | B | no | 1 | 1 d |
| S7 bounded execution | B | no | 2 | 3 d |
| S8 guardrails on tools | C | no | 1 | 1,5 d |

~12 days of work. The critical path is Track B (S6 → S7 → S8, ~5,5 d) and
nothing shortens it: those slices share two files and must be serial.

## What parallelism can and cannot compress

Track A's five slices after S0 are file-disjoint and can run wide. Track B cannot
be compressed at all, and attempting it produces conflicting hunks in
`completion_service.py` instead of throughput — Plan 17 recorded the same
constraint, and Round 15 confirmed it: seven of nineteen findings lived in those
two files.

So the useful shape is: **S0 alone, then a wide fan-out across S1–S5 while one
owner walks S6 → S7 → S8**, with OpenAPI regeneration and the full gate run once
per integration (D8) rather than per branch. Verification stays serial by nature —
`pytest` is ~4 minutes and `just ui-schema-check` is only meaningful against an
integrated tree.
