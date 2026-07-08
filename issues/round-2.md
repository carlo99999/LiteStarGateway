# Round 2 — Enterprise-readiness review (2026-07-02)

[← Index](INDEX.md)

Second whole-codebase pass focused on the "enterprise-ready LLM gateway" goal, run by four
parallel reviewers (security, Python/async, architecture, general quality) and verified against
source. Baseline is healthy: **188 tests pass**, `ruff` and `pyrefly` clean, no committed
secrets (`.env`/`*.db` are gitignored, placeholders only), no `TODO`/`print` residue.
IDs continue the round-1 sequence to avoid collision.

New counts: **0 CRITICAL · 7 HIGH · 7 MEDIUM · 4 LOW** + an enterprise-gap roadmap.

## Resolution status (Round 2, updated after remediation)

**Fixed (PRs open/merged):**

| Finding | Fix PR |
|---|---|
| H6 — upstream errors mapped to 429/502/504 (+`Retry-After`) | #68 |
| H7 — Argon2 offloaded to a worker thread | #69 |
| H8 — Vertex `genai.Client` closed per call | #70 |
| H9 — `--forwarded-allow-ips` defaults to loopback (`FORWARDED_ALLOW_IPS` opt-in) | #71 |
| H10 — audit on model + org/team lifecycle | #72 |
| H12 — bootstrap-admin replica race tolerated (unique-email arbiter) | #73 |
| M14 — error traces for failed LLM calls (incl. mid-stream) | #74 |
| M15 — `MASTER_KEY` strength/placeholder validation | #75 |
| M16 — OIDC `nonce` + PKCE (S256) | #76 |
| M17 — pagination on org/membership/usage-aggregate chains | #77 |
| L8 + L9 + L10 — coverage gate, invite/reset rate limit, rename | #78 |

**H11** was already resolved before this remediation pass: PR #67 added a
cross-replica distributed lock around the rotation loop (`guarded_rotate`), so
only one replica rotates. Recorded here as fixed-by-#67.

**Deferred (larger refactor or product decision needed):**

- **M18** — Postgres as the image default: a deploy-breaking default change;
  needs a release note / major-version decision (SQLite default is documented).
- **M19** — long-lived provider SDK client reuse per (provider, credential):
  perf refactor with cache-invalidation concerns (credential rotation), not a
  correctness bug now that all clients are closed (H8/#70).
- **M20** — transactional + denied-attempt audit coverage: requires moving audit
  writes into the same unit of work as each action (repo-wide convention change,
  overlaps round-1 M11).
- **L7** — pagination metadata envelope: a breaking API-shape change for all
  list endpoints; decide together with API versioning.

### HIGH (Round 2)

#### H6 — Provider errors (429/5xx/timeout) are never mapped to client status codes

- **Where:** `app.py:165` (only `DomainError` registered); adapters in `infrastructure/llm/*`; `application/completion_service.py`
- **Issue:** Provider SDK exceptions (`openai.RateLimitError`, `openai.APIStatusError`, `anthropic.APIStatusError`, `google.genai` errors, `httpx.TimeoutException`) are not `DomainError` subclasses, so they fall through to Litestar's default handler and always return a generic **500**.
- **Impact:** A real upstream 429 is indistinguishable from a gateway bug, which **breaks the client SDK's own retry/backoff** (it keys off the status code). A provider 503 surfaces as an opaque 500 with no `Retry-After`. No test exercises this path.
- **Fix:** Catch status-bearing SDK exceptions at the adapter/gateway boundary, wrap in new `DomainError` subclasses (`UpstreamRateLimited`→429, `UpstreamUnavailable`→502/503, `UpstreamTimeout`→504), register them in `exception_handlers._STATUS`, and add tests that mock each adapter raising them.

#### H7 — Argon2 password hashing/verification blocks the event loop

- **Where:** `domain/password.py:14-19`, called from `application/user_service.py:79,137,146,197,270`
- **Issue:** `hash_password`/`verify_password` (pwdlib Argon2, CPU-bound, ~50-200ms) are called **directly** inside `async def` methods (`ensure_admin`, `register`, `authenticate`, SSO user resolve, `reset_password`) with no thread offload.
- **Impact:** Under concurrent login/signup/reset traffic the single event loop stalls, delaying every other in-flight request including active LLM streams.
- **Fix:** `await anyio.to_thread.run_sync(hash_password, ...)` — the pattern already exists in `observability/dispatcher.py:41`.

#### H8 — `VertexAdapter` never closes its `genai.Client` (per-request connection leak)

- **Where:** `infrastructure/llm/vertex_adapter.py:187-257` (all methods)
- **Issue:** Every method builds a fresh `genai.Client` via `self._client(credentials)` and never calls `.close()` / `.aio.aclose()`. The round-1 H5/M12 fix (PR #52) closed OpenAI/Anthropic clients in `finally` but **Vertex was missed** — it is the one adapter that still leaks httpx pools/sockets on every call, sync or async.
- **Impact:** Sustained Vertex traffic accumulates sockets/FDs until GC → `Too many open files` on the worker.
- **Fix:** Mirror the `try/finally` used in the other adapters — `client.close()` for sync paths and `await client.aio.aclose()` for async paths.

#### H9 — Insecure-by-default `--forwarded-allow-ips "*"` → rate-limit bypass + audit spoofing

- **Where:** `docker-entrypoint.sh:8-10`; interacts with `infrastructure/web/rate_limit.py`, `audit/recorder.py:36`, `session/sso.py:60`
- **Issue:** The shipped production entrypoint trusts `X-Forwarded-For`/`X-Forwarded-Proto` from **any** peer. A direct client can forge a fresh `X-Forwarded-For` per request, getting a new rate-limit bucket each time and **completely bypassing** `AUTH_RATE_LIMIT` (20/min) on `/login`, `/signup`, `/reset-password`, and SSO → unthrottled brute force. Also falsifies the client IP recorded in the audit log and the scheme used for the SSO cookie `Secure` decision. (This elevates the deferred round-1 **L1** to a shipped-default HIGH.)
- **Impact:** Combined with M15, low-effort platform-admin account takeover; corrupted incident forensics.
- **Fix:** Ship a safe default — omit `--forwarded-allow-ips` (uvicorn ignores forwarded headers) or default to `127.0.0.1`/`::1`; require operators to opt in with their proxy's real IP/CIDR via env.

#### H10 — Audit log missing on model and organization/team creation

- **Where:** `infrastructure/web/models/controller.py` (create/update/delete_model), `infrastructure/web/organizations/controller.py` (create_organization, create_team)
- **Issue:** `record_audit` is wired into credentials/teams/set_active controllers but **not** into models or organizations. Model create/delete controls provider routing and per-token cost; org/team creation is a privileged state change. Both escape the append-only audit log shipped in the recent feature.
- **Impact:** Compliance gap — privileged, cost-affecting actions are unauditable.
- **Fix:** Add `record_audit(...)` to `create_model`/`update_model`/`delete_model`/`create_organization`/`create_team`, mirroring `teams/controller.py`; add a regression test asserting they appear in `/audit`.

#### H11 — Key-rotation loop runs on every replica (no leader election)

- **Where:** `infrastructure/rotation.py:79-99`, wired at `app.py:162`
- **Issue:** The daily rotation loop is started in every replica's lifespan. With N replicas you get N concurrent rotations at `KEY_ROTATION_TIME`, amplifying the round-1 **M4** non-atomic rotation race across processes.
- **Impact:** Concurrent multi-process rotation can transiently break the "exactly one active credential key" invariant and thrash re-encryption.
- **Fix:** Guard with a single-runner mechanism — DB advisory lock / leader election / external scheduler (cron job) instead of an in-process loop.

#### H12 — Bootstrap admin runs on every replica at startup (TOCTOU)

- **Where:** `app.py:161` (`on_startup`), `infrastructure/bootstrap.py:32-40`
- **Issue:** `make_bootstrap_admin` runs as an `on_startup` hook in every replica; N replicas racing `ensure_admin` on an empty users table is a cross-process TOCTOU.
- **Fix:** Rely on a unique constraint / upsert, or move bootstrap to a one-shot init job rather than per-replica startup.

### MEDIUM (Round 2)

#### M14 — Failed/erroring LLM calls are invisible in observability

- **Where:** `application/completion_service.py:73-119` (`_observe`); `infrastructure/observability/mlflow_sink.py:54-57`
- **Issue:** `_observe` runs only on the success path and hardcodes `status="ok"`. No code path ever builds a `TraceRecord(status="error")`, even though the sink already branches on it (dead branch). When the gateway call raises, `_observe` is skipped entirely — **no trace at all**. `_metered_stream`'s `finally` also records `status="ok"` even when the stream raised mid-way.
- **Impact:** Provider outages, timeouts, rate-limiting, and misconfiguration are invisible in tracing — exactly the events operators most need to see.
- **Fix:** Wrap gateway dispatch in each `CompletionService` method with try/except that emits `TraceRecord(status="error", ...)` (best-effort zeroed usage + latency + error type) before re-raising. Pairs with H6.

#### M15 — `MASTER_KEY` has no strength/default validation

- **Where:** `config.py:142-161`; `application/user_service.py:68-83`
- **Issue:** `__post_init__` enforces `MIN_SECRET_LENGTH` and non-default values for `jwt_secret`/`salt_key` outside local envs, but never validates `master_key`. `.env.sample` ships `MASTER_KEY=change-me-please`; only presence is checked (`MasterKeyMissing`), not strength, so a forgotten override creates the platform admin with `hash_password("change-me-please")`.
- **Impact:** With H9, fast low-effort platform-admin takeover.
- **Fix:** Apply the same length/non-default check to `master_key` in non-local environments, or at minimum reject the sample placeholder unconditionally.

#### M16 — OIDC flow omits PKCE and `nonce`

- **Where:** `infrastructure/sso/oidc.py:8-15`, `infrastructure/web/session/sso.py`
- **Issue:** `state` correctly covers CSRF, but there is no `nonce` (binding the `id_token` to this authorization request) and no PKCE (`code_verifier`/`code_challenge`). Documented as a known follow-up. Confidential-client code flow reduces practical risk, but this is defense-in-depth against ID-token replay/injection and authorization-code interception.
- **Fix:** Add `nonce` generation/verification (store alongside `state`) and PKCE — Authlib supports both natively.

#### M17 — Remaining unbounded list queries (pagination drift)

- **Where:** `persistence/organization_repository.py:29` (`list`), `membership_repository.py:40` (`list_by_team`, no limit/offset params at all), `usage_repository.aggregate`; callers `organizations/controller.py:38`, `teams/controller.py:40,199`
- **Issue:** Round-1 **M5** added pagination to credential/model/key lists, but these three chains were left unbounded — copy-paste drift against `pagination.py`'s own "list queries always run with a limit" contract.
- **Fix:** Add `limit`/`offset` matching the sibling repositories and wire `resolve_page` in the three handlers.

#### M18 — SQLite is the production default in the shipped image

- **Where:** `Dockerfile:33-35` (`ENVIRONMENT=production` + `DATABASE_URL=sqlite+aiosqlite:////data/gateway.db`)
- **Issue:** A stock `docker run` boots a "production" gateway on single-writer, volume-bound SQLite — no HA, no horizontal scaling. Postgres only via the compose override. The zero-dependency default and the production flag contradict each other.
- **Fix:** Make Postgres the image default and demote SQLite to an explicit dev opt-in.

#### M19 — Provider SDK client rebuilt per request (no connection reuse)

- **Where:** `infrastructure/llm/openai_adapter.py:69-76,109-125` (and siblings)
- **Issue:** Even with the leak fixed, a fresh client is built and closed per call, so every inference pays a TCP+TLS handshake to the upstream. Measurable latency/overhead at enterprise QPS.
- **Fix:** Reuse a long-lived client per (provider, credential).

#### M20 — Audit log is best-effort and incomplete for compliance

- **Where:** `infrastructure/web/audit/recorder.py`
- **Issue:** Audit entries are written **after** the action, not in the same transaction, and the log records neither login/SSO events nor **failed/denied** attempts. A SOC2/compliance auditor expects atomicity and denied-attempt capture.
- **Fix:** Write the audit entry in the same transaction as the action it describes; add login/SSO and authorization-denied events.

### LOW (Round 2)

- **L7 — List endpoints return bare arrays with no pagination metadata** (all `list_*` handlers). Clients can't tell whether more pages exist beyond the returned page (must guess from `len == limit`). *Fix: envelope with `total`/`limit`/`offset` if pagination is meant to be client-usable.*
- **L8 — No coverage tooling / gate.** `pytest-cov` isn't a declared dev dependency, so `pytest --cov` fails and the stated 80% bar can't be measured or gated in CI. *Fix: add `pytest-cov` to dev deps and a `--cov-fail-under` gate.*
- **L9 — No rate limit on `POST /invites` and `POST /password-resets`** (`web/users/invites.py`, `password_reset.py`). Admin-gated (low impact), but inconsistent with login/signup/reset. *Fix: apply `build_auth_rate_limit()` for consistency.*
- **L10 — Name-mangled `__check_if_password_has_some_complexity`** (`application/user_service.py:98`). Double-underscore triggers name mangling; the rest of the file uses single-underscore internals. *Fix: rename to `_check_password_complexity`.*

### Enterprise-readiness gaps (product roadmap, not defects)

Prioritized order to move from "clean single-org gateway" to "enterprise multi-tenant gateway":

1. **Pre-call budget enforcement & quotas** — usage is recorded post-hoc only (`completion_service._observe`); there is no pre-dispatch spend check or hard per-team/per-key cap. Spend is unbounded until someone reads a report. Single most-requested enterprise control.
2. **Ops observability** — no RED metrics, no `/metrics` (MLflow is an experiment tracker, not an ops backend), no `/ready` probe verifying DB/Redis (only DB-less `/health` liveness at `app.py:169`), no request-id/correlation-id.
3. **Credential org-scoping (reconsider round-1 "by design")** — the global credential pool is fine for *secret confidentiality* but leaves cross-tenant **spend** blast radius unbounded in multi-org deployments: any org's team admin can bind a model to any `credential_id` and burn another org's quota (`model_service._validate_credential` checks only provider match). The round-1 nullable-`organization_id` enhancement should be **elevated to required** before selling as multi-org. Bounded today only by UUIDv4 unguessability.
4. **Identity lifecycle & RBAC** — only 3 roles (platform admin / team admin / member); no read-only/billing/auditor roles, no per-key model scoping or spend caps; SSO is JIT-only with no SCIM deprovisioning.
5. **Resilience & SLA** — no aggregate end-to-end request deadline (timeout × retries × backoff can stack to ~3 min), no circuit breaker (defensible until routing/failover exists), no load shedding / priority queues.
6. **Hardening backlog** — request body-size limit (DoS), security headers, API-key expiry/TTL, `GET /v1/models`, memory-hard KDF or enforced-random master key (round-1 H3), CI coverage gate + dependency/image scanning (`pip-audit`, `bandit`) + branch protection on `main`.
