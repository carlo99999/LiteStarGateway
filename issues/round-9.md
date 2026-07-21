# Code Review — Round 9 (2026-07-14)

[← Index](INDEX.md)

Ninth full pass, over the changes after Round 8 (`d2f86a4..d4c2871`), re-checked
against the current tree. The main focus is the new admin UI and the
organizations/teams, invites, per-team/per-key rate-limit and API-key rotation
features. Round 1–8 findings are not repeated.

The suite and static gates are green (`807 passed`; Ruff, Pyrefly, pre-commit,
pip-audit, ESLint and the Vite build all clean), but the happy-path tests don't
cover some interactions between RBAC, rotation, user deactivation and the new
foreign keys. The most important findings were reproduced end-to-end with
`AsyncTestClient` against SQLite with foreign keys enabled — i.e. with the same
integrity semantics expected on Postgres.

Counts: **1 CRITICAL · 4 HIGH · 6 MEDIUM · 2 LOW**.

## Executive summary

The most urgent issue is a verified privilege escalation: a user with the
`key-issuer` role can rotate a service-principal key with `management` scope,
receive the new plaintext, and use it for management operations — bypassing the
`service-principals:manage` permission the original issue endpoint requires.

Rotation also introduces two regressions in the user kill switch: it changes the
owner of a rotated personal key, and it leaves the old key active for up to an
hour when its owner is deactivated. The new user deletion, finally, lets the
current admin delete themselves — even when they are the last platform admin.

Functionally: the per-key rate limit is skipped by embeddings and images;
invites make a team undeletable and a non-existent `team_id` produces a 500; the
admin UI truncates every collection to the first 100-row page. The UI also puts
credentials on two easily-persisted surfaces: the admin JWT in `localStorage`
and the invite token in the query string.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-001 | `key-issuer` can rotate a service-principal key and obtain management scope | critical | `infrastructure/web/teams/controller.py:335-359`; `application/service.py:117-140` | **Fixed** ([#249](https://github.com/carlo99999/LiteStarGateway/pull/249)) |
| ISSUE-002 | Rotation transfers a personal key's ownership to the operator | high | `application/service.py:117-140`; `infrastructure/web/teams/controller.py:335-359` | **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250)) |
| ISSUE-003 | User deactivation doesn't immediately revoke the old key in its grace window | high | `application/service.py:139`; `persistence/repository.py:85-95` | **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250)) |
| ISSUE-004 | The per-key rate limit is bypassable on embeddings and images | high | `application/completion_service.py:423-451,562-594` | **Fixed** ([#251](https://github.com/carlo99999/LiteStarGateway/pull/251)) |
| ISSUE-005 | An admin can delete themselves and leave the platform with no admin | high | `application/user_service.py:186-204` | **Fixed** ([#252](https://github.com/carlo99999/LiteStarGateway/pull/252)) |
| ISSUE-006 | Any persisted invite makes the team undeletable | medium | `persistence/team_repository.py:85-101`; `orm.py:112-130` | **Fixed** ([#253](https://github.com/carlo99999/LiteStarGateway/pull/253)) |
| ISSUE-007 | Creating an invite for a non-existent team returns 500 | medium | `application/user_service.py:160-177`; `persistence/invite_repository.py:20-32` | **Fixed** ([#253](https://github.com/carlo99999/LiteStarGateway/pull/253)) |
| ISSUE-008 | The invite token in the query string ends up in logs and history | medium | `ui/src/features/users/InviteUserDialog.tsx:56-59`; `SignupPage.tsx:11-17` | **Fixed** ([#254](https://github.com/carlo99999/LiteStarGateway/pull/254)) |
| ISSUE-009 | The admin UI shows only the first 100 records of every collection | medium | `ui/src/features/*/api.ts` | **Fixed** ([#259](https://github.com/carlo99999/LiteStarGateway/pull/259)) |
| ISSUE-010 | The admin JWT in `localStorage` is readable by same-origin scripts | medium | `ui/src/features/auth/AuthProvider.tsx:12-23` | **Fixed** ([#255](https://github.com/carlo99999/LiteStarGateway/pull/255)) |
| ISSUE-011 | Rotation isn't atomic and can leave an orphaned replacement key | medium | `application/service.py:128-140`; `persistence/repository.py:22-39,75-83` | **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250)) |
| ISSUE-012 | The in-memory rate limiter never evicts idle buckets | low | `infrastructure/rate_limiter.py:27-42` | **Fixed** ([#258](https://github.com/carlo99999/LiteStarGateway/pull/258)) |
| ISSUE-013 | The UI turns any budget error into "no budget" | low | `ui/src/features/teams/api.ts:43-49` | **Fixed** ([#257](https://github.com/carlo99999/LiteStarGateway/pull/257)) |

## Findings

### ISSUE-001 — `key-issuer` can rotate a service-principal key and obtain management scope (critical)

**Problem.** The service-principal key issue endpoint correctly requires
`Permission.SERVICE_PRINCIPALS_MANAGE`, while the new generic rotate endpoint
requires only `Permission.KEYS_ISSUE`. `rotate_for_team` copies `scope` and
`service_principal_id` from the source key without distinction and returns the
replacement's plaintext.

**Verified impact.** A user in the `key-issuer` role can list the team's keys,
pick a service-principal key with `management` scope, call rotate and receive a
new management key. End-to-end reproduction: rotate `201`, response with
`scope="management"` and plaintext; the new plaintext then got `200` on
`GET /teams/{id}/usage`, a management endpoint the `key-issuer`'s JWT can't
authorize directly. It's a privilege escalation from a limited role to every
team permission management keys carry.

**Suggested fix.** Authorize rotate by the source key's type: a key tied to a
service principal must require `SERVICE_PRINCIPALS_MANAGE` (ideally routed
through `ServicePrincipalService`); `KEYS_ISSUE` can remain sufficient only for
inference-only personal keys. Add a negative RBAC test with a `key-issuer` actor.

### ISSUE-002 — Rotation transfers a personal key's ownership to the operator (high)

**Problem.** The method promises a replacement with the same owner, but the
controller passes `current_user.id` and the service uses it as `created_by`. If
an admin or another key issuer rotates Alice's personal key, the new key belongs
to the operator, not to Alice.

**Verified impact.** After an admin-performed rotation, the key list shows
`created_by=admin` for the replacement. Deactivating Alice only revokes personal
keys with `created_by=Alice`, so the replacement keeps authenticating
indefinitely: the documented personal-key kill switch is bypassed and
ownership/audit attribution is falsified.

**Suggested fix.** A rotation must always use `key.created_by`; the operator's
identity stays in the audit event, it must not become the credential's owner.
Cover the "Alice issues, admin rotates, Alice is deactivated" case.

### ISSUE-003 — User deactivation doesn't immediately revoke the old key in its grace window (high)

**Problem.** Rotate sets a future `revoked_at` on the old key. The bulk revoke
run by user deactivation, however, updates only rows with `revoked_at IS NULL`,
so it ignores every key already in its grace window.

**Verified impact.** Deactivating the owner right after a rotation, the old
plaintext keeps getting `200 /whoami` until the grace hour expires.
Deactivation is explicitly the kill switch for sessions and personal keys; a
compromised key must not stay valid for an hour just because it was already
scheduled.

**Suggested fix.** The per-user revoke must also bring forward future
revocations (`revoked_at IS NULL OR revoked_at > now`) to `now`, leaving only
already-expired keys untouched.

### ISSUE-004 — The per-key rate limit is bypassable on embeddings and images (high)

**Problem.** The new `api_key_id` parameter of `_prepare` is optional and only
chat/responses pass it. `embeddings()` and `images()` call `_prepare` without
the id, so `UsageMeter._enforce_key_rate_limit` sees `None` and skips the
per-key bucket. The team limit and the per-IP pre-auth limit stay active, but
the key's configured limit isn't applied.

**Impact.** A key with `rate_limit_rpm=1` is blocked on the second chat request,
but can do embeddings or image generations up to the global per-IP limit
(120/min), bypassing the per-key control and its cost containment.

**Suggested fix.** Pass `api_key_id` to `_prepare` in both methods and add a
rate-limit matrix over chat, responses, embeddings, images and the two native
surfaces.

### ISSUE-005 — An admin can delete themselves and leave the platform with no admin (high)

**Problem.** `set_user_admin` and `set_user_active` explicitly protect self
operations, but the new `delete_user` doesn't check `actor.id == user_id`. If
the admin has no memberships or created keys, they can delete their own account.

**Impact.** The only platform admin can delete themselves, leaving non-admin
accounts in the database. Bootstrap won't recreate the admin because
`users.count() > 0`, so organizations, credentials, invites and governance
become unreachable without direct DB intervention. The UI disables the
self-delete button, but the protection must live in the service/API.

**Suggested fix.** Refuse self-delete and keep the "at least one platform admin"
invariant in the same transaction as the deletion.

### ISSUE-006 — Any persisted invite makes the team undeletable (medium)

**Problem.** The new FK `invite.team_id -> team.id` doesn't specify `ondelete`
and so uses the default `NO ACTION`; `TeamRepository.delete` removes every
"intrinsic" child except `InviteModel`. Even used or expired invites stay in the
table and keep blocking the delete.

**Verified impact.** After a single `POST /invites`, `DELETE /teams/{id}` reaches
the SQL delete and returns 500 `FOREIGN KEY constraint failed`. Since there's no
invite-cleanup endpoint, that team can no longer be deleted via the API.

**Suggested fix.** Define the invite lifecycle explicitly: delete them in the
repository before the team, or use `ON DELETE CASCADE`; alternatively treat them
as a blocking reference and return a handleable 409, with a way to remove them.

### ISSUE-007 — Creating an invite for a non-existent team returns 500 (medium)

**Problem.** `create_invite` doesn't check the team exists before inserting the
new FK, and the repository doesn't translate the `IntegrityError` into a domain
error.

**Verified impact.** A platform admin who sends a non-existent UUID gets a 500
with DB rollback instead of a 404/400. Reproduced with SQLite+FK; the same
referential violation is expected on Postgres. The endpoint stays fragile to
stale input (e.g. a UI left open while the team is deleted).

**Suggested fix.** Resolve the team in the service before issuing the invite and
return `TeamNotFound`; keep an `IntegrityError` translation anyway for the
delete-vs-create race.

### ISSUE-008 — The invite token in the query string ends up in logs and history (medium)

**Problem.** The UI generates `/ui/signup?token=<bearer>` links and the page
reads the token from the query. The full initial request is normally recorded by
the Uvicorn/reverse-proxy access log and stays in the browser history; external
navigations may also propagate it via `Referer` if the policy changes.

**Impact.** Anyone who can read access logs or history obtains a single-use
credential valid for 72 hours and can create the account before the invitee. The
token is hashed in the DB, but the URL reintroduces the plaintext on persistent
surfaces.

**Suggested fix.** Carry the token in the fragment (`#token=...`, never sent to
the server) or strip it from the bar immediately with `history.replaceState`
before any other activity; set a restrictive `Referrer-Policy`.

### ISSUE-009 — The admin UI shows only the first 100 records of every collection (medium)

**Problem.** The endpoints are correctly paginated with a default of 100, but
every UI client omits `limit`/`offset` and never requests further pages. The
tables don't even indicate the result is truncated.

**Impact.** From the 101st organization/team/user/member/key/usage model onward,
records disappear from the console and can't be selected for invites, rotation,
revoke or administration. The data exists and the API exposes it, but the
operator sees a false complete list.

**Suggested fix.** Implement pagination/infinite query in the tables and pickers,
or iterate the pages to exhaustion for datasets that must be complete. Round 2's
deferred L7 (missing metadata) makes it worth adding `total/next_offset` too.

### ISSUE-010 — The admin JWT in `localStorage` is readable by same-origin scripts (medium)

**Problem.** The new console stores the platform admin's bearer JWT in
`localStorage`. Any script that can run on the origin (a future XSS, a
compromised bundle/dependency, a page-access extension) can read it and send it
off-process; the token has global privileges and a long lifetime.

**Impact.** The scenario requires same-origin JavaScript execution, but in that
case there's no `HttpOnly` protection and the theft survives reload/browser
restart. A single XSS in the console becomes full gateway compromise.

**Suggested fix.** For the browser console use an `HttpOnly; Secure;
SameSite=Strict` session cookie with CSRF protection, leaving bearer JWTs for
API/CLI use. As an interim mitigation, memory/sessionStorage + a strict CSP
reduce persistence and surface.

### ISSUE-011 — Rotation isn't atomic and can leave an orphaned replacement key (medium)

**Problem.** `rotate_for_team` calls `issue()` (which commits the replacement)
then `update()` (a second commit on the old key). An error/crash between the two
steps can't be rolled back.

**Impact.** The client gets a 500 and no plaintext, but an extra active key
stays in the DB while the old one doesn't enter its grace window. Retries create
more keys; the inventory and audit don't represent a single atomic rotation.

**Suggested fix.** Stage the insert and update in the same unit of work /
transaction and commit once; return the plaintext only after a successful commit.

### ISSUE-012 — The in-memory rate limiter never evicts idle buckets (low)

**Problem.** `_counts` replaces a bucket only when the same key sends traffic
again, but never removes teams/keys that will never be used again. Revocations,
rotations and deletes don't notify the limiter.

**Impact.** In the single-process fallback without Redis, a long-lived gateway
with API-key churn accumulates one entry per limited key/team ever seen. Memory
grows with the historical count, not with the active callers as the comment
claims.

**Suggested fix.** Periodic/lazy pruning of old buckets or a bounded TTL cache;
the Redis path is already correct because it uses `EXPIRE`.

### ISSUE-013 — The UI turns any budget error into "no budget" (low)

**Problem.** `getTeamBudget` returns `null` for any `error || !data`, not just
the 404 `BudgetNotFound`. A 401/403, a 500 or a network error become
indistinguishable from "no budget configured".

**Impact.** During an auth/DB problem the team page reports false governance
information and offers no retry/error context to the operator.

**Suggested fix.** Map only a 404 response to `null` and re-raise other errors so
React Query shows the error state.

## Resolution status — FULLY REMEDIATED

Remediation started with the critical finding. **ISSUE-001** is fixed by
[#249](https://github.com/carlo99999/LiteStarGateway/pull/249): rotate first
resolves the active key in the team and, for any key tied to a service
principal, also requires `SERVICE_PRINCIPALS_MANAGE` before issuing the
replacement. The RBAC tests cover `inference`, `management` and `all` scopes,
verify the denial doesn't mutate the key, and that the authorized replacement
stays subject to the SP kill switch.

**ISSUE-002**, **ISSUE-003** and **ISSUE-011** are fixed together by
[#250](https://github.com/carlo99999/LiteStarGateway/pull/250): rotation keeps
the original owner and commits the replacement, grace and audit in one unit of
work; deactivation serializes on the owner and brings forward future
revocations too. A final DB validation also stops auth/telemetry races from
accepting revoked credentials. The PostgreSQL tests cover rotate/deactivate,
concurrent rotations, the throttled fast path vs revoke, and SP deletion.

**ISSUE-004** is fixed by
[#251](https://github.com/carlo99999/LiteStarGateway/pull/251): embeddings and
images propagate the key id to the RPM gate, while `_prepare` requires it and
applies the limit before any billable routing strategies. The tests cover both
direct endpoints and the denial before the judge's provider call.

**ISSUE-005** is fixed by
[#252](https://github.com/carlo99999/LiteStarGateway/pull/252): admin
lifecycle operations are serialized and refuse self-delete, self-demote and
self-disable, preserving at least one administrative access even under races.

**ISSUE-006** and **ISSUE-007** are fixed together by
[#253](https://github.com/carlo99999/LiteStarGateway/pull/253): create, redeem
and delete share a team lifecycle lock; invites are deleted with the team and
stale references become domain 404/409s. Registration, single-use consumption
and membership are atomic, including expiry and concurrent email conflicts.

**ISSUE-008** is fixed by
[#254](https://github.com/carlo99999/LiteStarGateway/pull/254): links carry the
bearer only in the fragment, which is captured and removed from history before
the router. The token stays in memory for retries, is cleared after signup, and
the SPA sets `Referrer-Policy: no-referrer`; legacy query links are redacted but
no longer accepted.

**ISSUE-009** is fixed by
[#259](https://github.com/carlo99999/LiteStarGateway/pull/259): the tables use
server-side pages with a sentinel and deterministic DB order; pickers and the
usage total exhaust bounded collections sequentially, cancellable and with no
partial results. The global keys view selects a team and pages a single list,
eliminating the team × key fan-out, while errors stay visible and data caches
are cleared on session changes.

**ISSUE-010** is fixed by
[#255](https://github.com/carlo99999/LiteStarGateway/pull/255): the console uses
a host-only `HttpOnly; SameSite=Strict` cookie session with CSRF bound to the
JWT and a same-origin check, while bearers and API keys stay isolated for
CLI/SDK and inference. The JWT is never exposed to JavaScript; HTTPS uses the
`__Host-` prefix, insecure non-local configs fail at startup, and the legacy
token is only deleted from `localStorage` during bootstrap.

**ISSUE-012** is fixed by
[#258](https://github.com/carlo99999/LiteStarGateway/pull/258): the in-memory
limiter lazily evicts expired counters on the next traffic, preserving still-
active windows and concurrent serialization under the same lock. The Redis
namespace also includes the window duration, preventing count/TTL collisions
across different policies.

**ISSUE-013** is fixed by
[#257](https://github.com/carlo99999/LiteStarGateway/pull/257): the client maps
only a 404 response to `null`; auth, server, network and malformed responses
stay errors with useful messages. The team page now distinguishes `unavailable`
from `none` and renders the detail in an accessible alert.

## Verifications run

- `uv run pytest -q` → **807 passed**.
- `uv run ruff check src tests` → clean.
- `uv run pyrefly check` → 0 errors.
- `uv run pre-commit run --all-files --show-diff-on-failure` → all hooks pass.
- `uv run pip-audit` → no known vulnerabilities.
- `pnpm lint && pnpm build` in `ui/` → clean (only the Vite >500 kB chunk warning).
- API reproduction of the `key-issuer` → management-key privilege escalation.
- API reproduction of owner transfer + both keys still valid after deactivate.
- API reproduction of invite on a non-existent team → 500 FK.
- API reproduction of team delete after an invite → 500 FK.

## Category scores (this round)

| Category | Score | Summary |
|---|---:|---|
| Security / RBAC | **6/10** | Solid historical core, but rotate opens a concrete escalation and three kill-switch regressions. |
| Money / rate limiting | **7/10** | Team/key limiters well structured, but per-key doesn't cover embeddings/images. |
| Persistence / lifecycle | **6.5/10** | FKs and UoW generally careful; the invite lifecycle and the multi-commit rotate break two real workflows. |
| Admin UI | **6.5/10** | Typed, clean build, but token storage, pagination and error masking aren't production-ready. |
| Test / CI | **8.5/10** | 807 green tests and strong gates; missing the RBAC/rotation/FK interaction tests that would have caught the main findings. |

**Overall: 6.8/10.** The base stays good and verified. ISSUE-001 was closed by
[#249](https://github.com/carlo99999/LiteStarGateway/pull/249); ISSUE-002,
ISSUE-003 and ISSUE-011 by
[#250](https://github.com/carlo99999/LiteStarGateway/pull/250), and ISSUE-004 by
[#251](https://github.com/carlo99999/LiteStarGateway/pull/251). The next high
finding is now last-admin governance.
