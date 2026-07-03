# Code Review — Findings (`ISSUES.md`)

Whole-codebase security & quality review of `src/litestar_gateway` (≈6,300 LOC, hexagonal
architecture). Findings below were surfaced by layer-focused reviewers and then
**verified against the actual code** — each cites `file:line`, the concrete impact, and a
suggested fix. Severity reflects verified exploitability/impact, not the raw finder claim.

**Overall:** the codebase is solid. No SQL injection (all queries parameterized), no JWT
algorithm confusion (`algorithms=[HS256]` pinned), constant-time secret comparison, routes
correctly guarded, tenant scoping enforced for models/API keys, and no obvious secret leakage
in response DTOs. The issues below are real but mostly hardening, multi-tenancy, and
robustness gaps rather than open doors. See the bottom section for things reviewers raised
that were verified as **not issues / by design**.

Counts: **1 CRITICAL · 5 HIGH · 13 MEDIUM · 6 LOW**.

---

## Resolution status (updated after remediation)

**Fixed & merged to `main`:**

| Finding | Fix PR |
|---|---|
| C1 — streaming unbilled/untraced | #51 |
| H1 — last-admin protection | #49 |
| H2 — invite expiry | #50 |
| H5 + M12 — provider SDK client leak / stream close | #52 |
| H3 (entropy) + H4 + M2 + M3 (strength) + M10 — config/secret hardening | #53 |
| M1 + M8 + M9 — JWT key window, Vertex credential errors, docs toggle | #54 |
| M6 — dropped billing events logged at ERROR (recoverable) | #56 |
| M5 — pagination on team-scoped list endpoints | #57 |
| M4 (reframed) — per-API-key spending report incl. revoked keys | #58 |

The former **H1 (credentials not org-scoped)** was reclassified **by design** — credentials are an intentionally global pool managed by cloud ops (see the by-design section below). **M4** was reframed from "rotation atomicity" to "see past keys + their spend" per the owner's clarification, and shipped as the per-key spending report (#58); the keyring rotation itself stays non-destructive (retired keys stay readable, ciphertexts are key-tagged), so its transactional refactor was not needed.

**Deferred (deliberately not changed — larger refactor or product decision needed):**

- **M7** — `Model.update` can't reset an optional field to `null`: needs explicit PATCH null-vs-omitted semantics (msgspec sentinel).
- **M11** — persistence transaction-boundary inconsistency: a repo-wide convention refactor.
- **M13** — no standalone adapter unit tests: the adapters are already heavily exercised by `test_completions.py` via mocked SDKs; dedicated unit tests of the pure translators are optional.
- **L1–L6** (LOW) — proxy-aware rate limiting, `tv`-claim strictness, admin-forced logout / account-disable, DTO format/length validation, explicit `204` on deletes, usage-table indexes. Ops/minor; see the LOW section.

---

# Round 2 — Enterprise-readiness review (2026-07-02)

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

## HIGH (Round 2)

### H6 — Provider errors (429/5xx/timeout) are never mapped to client status codes
- **Where:** `app.py:165` (only `DomainError` registered); adapters in `infrastructure/llm/*`; `application/completion_service.py`
- **Issue:** Provider SDK exceptions (`openai.RateLimitError`, `openai.APIStatusError`, `anthropic.APIStatusError`, `google.genai` errors, `httpx.TimeoutException`) are not `DomainError` subclasses, so they fall through to Litestar's default handler and always return a generic **500**.
- **Impact:** A real upstream 429 is indistinguishable from a gateway bug, which **breaks the client SDK's own retry/backoff** (it keys off the status code). A provider 503 surfaces as an opaque 500 with no `Retry-After`. No test exercises this path.
- **Fix:** Catch status-bearing SDK exceptions at the adapter/gateway boundary, wrap in new `DomainError` subclasses (`UpstreamRateLimited`→429, `UpstreamUnavailable`→502/503, `UpstreamTimeout`→504), register them in `exception_handlers._STATUS`, and add tests that mock each adapter raising them.

### H7 — Argon2 password hashing/verification blocks the event loop
- **Where:** `domain/password.py:14-19`, called from `application/user_service.py:79,137,146,197,270`
- **Issue:** `hash_password`/`verify_password` (pwdlib Argon2, CPU-bound, ~50-200ms) are called **directly** inside `async def` methods (`ensure_admin`, `register`, `authenticate`, SSO user resolve, `reset_password`) with no thread offload.
- **Impact:** Under concurrent login/signup/reset traffic the single event loop stalls, delaying every other in-flight request including active LLM streams.
- **Fix:** `await anyio.to_thread.run_sync(hash_password, ...)` — the pattern already exists in `observability/dispatcher.py:41`.

### H8 — `VertexAdapter` never closes its `genai.Client` (per-request connection leak)
- **Where:** `infrastructure/llm/vertex_adapter.py:187-257` (all methods)
- **Issue:** Every method builds a fresh `genai.Client` via `self._client(credentials)` and never calls `.close()` / `.aio.aclose()`. The round-1 H5/M12 fix (PR #52) closed OpenAI/Anthropic clients in `finally` but **Vertex was missed** — it is the one adapter that still leaks httpx pools/sockets on every call, sync or async.
- **Impact:** Sustained Vertex traffic accumulates sockets/FDs until GC → `Too many open files` on the worker.
- **Fix:** Mirror the `try/finally` used in the other adapters — `client.close()` for sync paths and `await client.aio.aclose()` for async paths.

### H9 — Insecure-by-default `--forwarded-allow-ips "*"` → rate-limit bypass + audit spoofing
- **Where:** `docker-entrypoint.sh:8-10`; interacts with `infrastructure/web/rate_limit.py`, `audit/recorder.py:36`, `session/sso.py:60`
- **Issue:** The shipped production entrypoint trusts `X-Forwarded-For`/`X-Forwarded-Proto` from **any** peer. A direct client can forge a fresh `X-Forwarded-For` per request, getting a new rate-limit bucket each time and **completely bypassing** `AUTH_RATE_LIMIT` (20/min) on `/login`, `/signup`, `/reset-password`, and SSO → unthrottled brute force. Also falsifies the client IP recorded in the audit log and the scheme used for the SSO cookie `Secure` decision. (This elevates the deferred round-1 **L1** to a shipped-default HIGH.)
- **Impact:** Combined with M15, low-effort platform-admin account takeover; corrupted incident forensics.
- **Fix:** Ship a safe default — omit `--forwarded-allow-ips` (uvicorn ignores forwarded headers) or default to `127.0.0.1`/`::1`; require operators to opt in with their proxy's real IP/CIDR via env.

### H10 — Audit log missing on model and organization/team creation
- **Where:** `infrastructure/web/models/controller.py` (create/update/delete_model), `infrastructure/web/organizations/controller.py` (create_organization, create_team)
- **Issue:** `record_audit` is wired into credentials/teams/set_active controllers but **not** into models or organizations. Model create/delete controls provider routing and per-token cost; org/team creation is a privileged state change. Both escape the append-only audit log shipped in the recent feature.
- **Impact:** Compliance gap — privileged, cost-affecting actions are unauditable.
- **Fix:** Add `record_audit(...)` to `create_model`/`update_model`/`delete_model`/`create_organization`/`create_team`, mirroring `teams/controller.py`; add a regression test asserting they appear in `/audit`.

### H11 — Key-rotation loop runs on every replica (no leader election)
- **Where:** `infrastructure/rotation.py:79-99`, wired at `app.py:162`
- **Issue:** The daily rotation loop is started in every replica's lifespan. With N replicas you get N concurrent rotations at `KEY_ROTATION_TIME`, amplifying the round-1 **M4** non-atomic rotation race across processes.
- **Impact:** Concurrent multi-process rotation can transiently break the "exactly one active credential key" invariant and thrash re-encryption.
- **Fix:** Guard with a single-runner mechanism — DB advisory lock / leader election / external scheduler (cron job) instead of an in-process loop.

### H12 — Bootstrap admin runs on every replica at startup (TOCTOU)
- **Where:** `app.py:161` (`on_startup`), `infrastructure/bootstrap.py:32-40`
- **Issue:** `make_bootstrap_admin` runs as an `on_startup` hook in every replica; N replicas racing `ensure_admin` on an empty users table is a cross-process TOCTOU.
- **Fix:** Rely on a unique constraint / upsert, or move bootstrap to a one-shot init job rather than per-replica startup.

## MEDIUM (Round 2)

### M14 — Failed/erroring LLM calls are invisible in observability
- **Where:** `application/completion_service.py:73-119` (`_observe`); `infrastructure/observability/mlflow_sink.py:54-57`
- **Issue:** `_observe` runs only on the success path and hardcodes `status="ok"`. No code path ever builds a `TraceRecord(status="error")`, even though the sink already branches on it (dead branch). When the gateway call raises, `_observe` is skipped entirely — **no trace at all**. `_metered_stream`'s `finally` also records `status="ok"` even when the stream raised mid-way.
- **Impact:** Provider outages, timeouts, rate-limiting, and misconfiguration are invisible in tracing — exactly the events operators most need to see.
- **Fix:** Wrap gateway dispatch in each `CompletionService` method with try/except that emits `TraceRecord(status="error", ...)` (best-effort zeroed usage + latency + error type) before re-raising. Pairs with H6.

### M15 — `MASTER_KEY` has no strength/default validation
- **Where:** `config.py:142-161`; `application/user_service.py:68-83`
- **Issue:** `__post_init__` enforces `MIN_SECRET_LENGTH` and non-default values for `jwt_secret`/`salt_key` outside local envs, but never validates `master_key`. `.env.sample` ships `MASTER_KEY=change-me-please`; only presence is checked (`MasterKeyMissing`), not strength, so a forgotten override creates the platform admin with `hash_password("change-me-please")`.
- **Impact:** With H9, fast low-effort platform-admin takeover.
- **Fix:** Apply the same length/non-default check to `master_key` in non-local environments, or at minimum reject the sample placeholder unconditionally.

### M16 — OIDC flow omits PKCE and `nonce`
- **Where:** `infrastructure/sso/oidc.py:8-15`, `infrastructure/web/session/sso.py`
- **Issue:** `state` correctly covers CSRF, but there is no `nonce` (binding the `id_token` to this authorization request) and no PKCE (`code_verifier`/`code_challenge`). Documented as a known follow-up. Confidential-client code flow reduces practical risk, but this is defense-in-depth against ID-token replay/injection and authorization-code interception.
- **Fix:** Add `nonce` generation/verification (store alongside `state`) and PKCE — Authlib supports both natively.

### M17 — Remaining unbounded list queries (pagination drift)
- **Where:** `persistence/organization_repository.py:29` (`list`), `membership_repository.py:40` (`list_by_team`, no limit/offset params at all), `usage_repository.aggregate`; callers `organizations/controller.py:38`, `teams/controller.py:40,199`
- **Issue:** Round-1 **M5** added pagination to credential/model/key lists, but these three chains were left unbounded — copy-paste drift against `pagination.py`'s own "list queries always run with a limit" contract.
- **Fix:** Add `limit`/`offset` matching the sibling repositories and wire `resolve_page` in the three handlers.

### M18 — SQLite is the production default in the shipped image
- **Where:** `Dockerfile:33-35` (`ENVIRONMENT=production` + `DATABASE_URL=sqlite+aiosqlite:////data/gateway.db`)
- **Issue:** A stock `docker run` boots a "production" gateway on single-writer, volume-bound SQLite — no HA, no horizontal scaling. Postgres only via the compose override. The zero-dependency default and the production flag contradict each other.
- **Fix:** Make Postgres the image default and demote SQLite to an explicit dev opt-in.

### M19 — Provider SDK client rebuilt per request (no connection reuse)
- **Where:** `infrastructure/llm/openai_adapter.py:69-76,109-125` (and siblings)
- **Issue:** Even with the leak fixed, a fresh client is built and closed per call, so every inference pays a TCP+TLS handshake to the upstream. Measurable latency/overhead at enterprise QPS.
- **Fix:** Reuse a long-lived client per (provider, credential).

### M20 — Audit log is best-effort and incomplete for compliance
- **Where:** `infrastructure/web/audit/recorder.py`
- **Issue:** Audit entries are written **after** the action, not in the same transaction, and the log records neither login/SSO events nor **failed/denied** attempts. A SOC2/compliance auditor expects atomicity and denied-attempt capture.
- **Fix:** Write the audit entry in the same transaction as the action it describes; add login/SSO and authorization-denied events.

## LOW (Round 2)

- **L7 — List endpoints return bare arrays with no pagination metadata** (all `list_*` handlers). Clients can't tell whether more pages exist beyond the returned page (must guess from `len == limit`). *Fix: envelope with `total`/`limit`/`offset` if pagination is meant to be client-usable.*
- **L8 — No coverage tooling / gate.** `pytest-cov` isn't a declared dev dependency, so `pytest --cov` fails and the stated 80% bar can't be measured or gated in CI. *Fix: add `pytest-cov` to dev deps and a `--cov-fail-under` gate.*
- **L9 — No rate limit on `POST /invites` and `POST /password-resets`** (`web/users/invites.py`, `password_reset.py`). Admin-gated (low impact), but inconsistent with login/signup/reset. *Fix: apply `build_auth_rate_limit()` for consistency.*
- **L10 — Name-mangled `__check_if_password_has_some_complexity`** (`application/user_service.py:98`). Double-underscore triggers name mangling; the rest of the file uses single-underscore internals. *Fix: rename to `_check_password_complexity`.*

## Enterprise-readiness gaps (product roadmap, not defects)

Prioritized order to move from "clean single-org gateway" to "enterprise multi-tenant gateway":

1. **Pre-call budget enforcement & quotas** — usage is recorded post-hoc only (`completion_service._observe`); there is no pre-dispatch spend check or hard per-team/per-key cap. Spend is unbounded until someone reads a report. Single most-requested enterprise control.
2. **Ops observability** — no RED metrics, no `/metrics` (MLflow is an experiment tracker, not an ops backend), no `/ready` probe verifying DB/Redis (only DB-less `/health` liveness at `app.py:169`), no request-id/correlation-id.
3. **Credential org-scoping (reconsider round-1 "by design")** — the global credential pool is fine for *secret confidentiality* but leaves cross-tenant **spend** blast radius unbounded in multi-org deployments: any org's team admin can bind a model to any `credential_id` and burn another org's quota (`model_service._validate_credential` checks only provider match). The round-1 nullable-`organization_id` enhancement should be **elevated to required** before selling as multi-org. Bounded today only by UUIDv4 unguessability.
4. **Identity lifecycle & RBAC** — only 3 roles (platform admin / team admin / member); no read-only/billing/auditor roles, no per-key model scoping or spend caps; SSO is JIT-only with no SCIM deprovisioning.
5. **Resilience & SLA** — no aggregate end-to-end request deadline (timeout × retries × backoff can stack to ~3 min), no circuit breaker (defensible until routing/failover exists), no load shedding / priority queues.
6. **Hardening backlog** — request body-size limit (DoS), security headers, API-key expiry/TTL, `GET /v1/models`, memory-hard KDF or enforced-random master key (round-1 H3), CI coverage gate + dependency/image scanning (`pip-audit`, `bandit`) + branch protection on `main`.

---

## CRITICAL

### C1 — Streaming inference is completely unbilled and untraced
- **Where:** `application/completion_service.py:143-159` (`open_chat_stream`, `open_responses_stream`); `infrastructure/web/api_router/completions.py:58-61, 82-84`
- **Issue:** The non-streaming paths call `_observe(...)` to persist a `UsageEvent` (billing) and emit a trace. The streaming paths return the provider iterator directly and **never call `_observe`**, and the controller's SSE wrappers only serialize chunks. No usage row, no cost, no trace is recorded for any `stream: true` request.
- **Impact:** A customer who sets `stream: true` (the default for many OpenAI clients) consumes upstream provider tokens with **zero billing and zero observability**. Usage aggregates under-report; cost attribution is bypassed entirely.
- **Fix:** Accumulate token usage while iterating the stream (request `stream_options: {include_usage: true}` for OpenAI-compatible providers so the final chunk carries `usage`) and call `_observe` when the stream completes; wrap the generator so it records even on early client disconnect.

---

> **Note:** the earlier H1 ("credentials not scoped to an org/team") was resolved as
> **by design** after a product decision — credentials are an intentionally global pool
> managed centrally by cloud ops, and all orgs pull from the same set. See the
> "NOT issues / by design" section below. HIGH items renumbered accordingly.

## HIGH

### H1 — No "last admin" protection: a team admin can orphan a team
- **Where:** `application/team_service.py:147-156` (`set_role`), `158-163` (`remove_member`)
- **Issue:** Neither method guards against demoting/removing the **last** admin (or the platform admin's own membership on the team).
- **Impact:** A team admin can `DELETE`/demote every other admin, leaving the team with no team-level administrator (recoverable only by a platform admin), or strip the platform admin's membership to reduce oversight.
- **Fix:** Before demote/remove of an ADMIN membership, assert at least one other ADMIN remains; forbid a team admin from removing the platform admin's membership.

### H2 — Invite tokens never expire
- **Where:** `domain/entities.py` `Invite.is_usable` (checks only `used_at`); `application/user_service.py:81-90` (`create_invite`)
- **Issue:** Invites are single-use but have **no `expires_at`**. A leaked invite (logs, chat, screenshot) is a valid registration path indefinitely.
- **Impact:** A captured invite token can be redeemed months later to create an account.
- **Fix:** Add `expires_at` (mirror `PasswordReset`, e.g. 24-72h TTL) and check it in `is_usable`; add a migration + column.

### H3 — Master key derived from a single unsalted SHA-256 pass
- **Where:** `infrastructure/crypto.py:20-22` (`_derive_fernet_key`)
- **Issue:** The envelope-encryption master key (from `SALT_KEY` / `JWT_SECRET`) is `base64(sha256(secret))` — no salt, no slow KDF. This is fine for a **high-entropy random** master, but offers no protection if an operator uses a human-chosen passphrase.
- **Impact:** If `SALT_KEY` is a low-entropy passphrase and the DB keyring is exfiltrated, an attacker brute-forces it offline at GPU speed, unwraps every data key, and decrypts all stored provider credentials.
- **Fix:** Either enforce/validate high-entropy keys at startup (length + randomness), or derive via a memory-hard KDF (scrypt/argon2) with a stored per-deployment salt. At minimum, document the requirement that these be random 32-byte values.

### H4 — Default JWT secret is only rejected for exactly `production`/`prod`
- **Where:** `config.py:34` (`_PRODUCTION_ENVIRONMENTS`), `88-93` (`__post_init__`)
- **Issue:** The publicly-known `DEFAULT_JWT_SECRET` is only rejected when `ENVIRONMENT` is exactly `production`/`prod`. Any other internet-facing value (`staging`, `prod ` with a space, a typo like `prodction`) silently signs JWTs with the git-committed default.
- **Impact:** An attacker who knows the default secret forges an admin JWT and takes over the gateway.
- **Fix:** Invert the default — require an explicitly-set strong `JWT_SECRET` **unless** environment is a known-local value (`development`/`test`), or fail fast on the default secret in every non-local environment. Trim/normalize `ENVIRONMENT`.

### H5 — Provider SDK clients are created per request and never closed
- **Where:** `infrastructure/llm/openai_adapter.py:64-130, 143-148`; `infrastructure/llm/anthropic_adapter.py` (async methods)
- **Issue:** Each call builds a fresh `AsyncOpenAI` / `AsyncAnthropic` (each owns an httpx connection pool) and never `aclose()`s it — the same leak pattern already fixed for the SSO `AsyncOAuth2Client`.
- **Impact:** Under sustained traffic, connection pools/sockets accumulate until GC, risking `Too many open files` on the worker; streamed responses that the client abandons also leave the SDK stream open.
- **Fix:** Reuse a long-lived client per (provider, credential) or wrap per-call clients in `async with`/`try…finally: await client.aclose()`; close streams on generator finalization.

---

## MEDIUM

### M1 — JWT signing keys can be deleted while dependent tokens are still valid
- **Where:** `infrastructure/keyring.py:84-91` (`rotate_jwt`)
- **Issue:** Keys are deleted when `created_at < now - max_age`, but a key is *active for signing* for up to one rotation interval after creation. A token signed late in a key's active window outlives the key's deletion by up to ~1 interval → premature "invalid token" mid-session.
- **Fix:** Use `created_at < now - (max_age + rotation_interval)` (or retire-then-delete based on when the key stopped being active).

### M2 — JWT master cipher accepts an empty secret (fixed, public key)
- **Where:** `infrastructure/keyring.py:39-42`; `infrastructure/crypto.py:33-34`
- **Issue:** `_master(JWT)` calls `MasterCipher(self._jwt_master_key)` with no empty check, unlike the credential master (`build_master_cipher` raises). `MasterCipher("")` derives a fixed key from `sha256("")`. Reachable in non-production if `JWT_SECRET=""` is set explicitly.
- **Fix:** Route the JWT master through a presence check too; combined with H5, validate `JWT_SECRET` in all non-local environments.

### M3 — `SALT_KEY` has no startup fail-fast
- **Where:** `config.py:88-93` (validates only `JWT_SECRET`)
- **Issue:** Unlike `JWT_SECRET`, missing `SALT_KEY` isn't caught at startup. The app boots healthy and credential operations later fail with `SaltKeyMissing` (503) only at first use. (Credentials are *not* written unencrypted — the operation fails closed — so this is a late-failure/ops issue, not a data-exposure one.)
- **Fix:** If credential features are expected in production, assert `SALT_KEY` presence in `__post_init__`.

### M4 — Key rotation is not atomic across steps
- **Where:** `infrastructure/rotation.py:49-55` (`rotate_all`); `infrastructure/keyring.py` create/retire; `credential_repository.reencrypt_all`
- **Issue:** `new_credential_key → reencrypt_all → retire_old → rotate_jwt` run as separate commits in one session. A crash mid-sequence leaves a partially-rotated keyring. This is **non-destructive** (retired keys stay readable and each ciphertext records its key id, so un-migrated rows still decrypt, and the next run re-encrypts), but the "exactly one active credential key" invariant can transiently break.
- **Fix:** Wrap the sequence in a single transaction, or make it idempotent/resumable and log partial-rotation state.

### M5 — Unbounded queries (no pagination)
- **Where:** `credential_repository.py:57` (`list`), `72-85` (`reencrypt_all`); `model_repository`/`api_key` `list_by_team`; `usage_repository.aggregate`
- **Issue:** These `SELECT`s have no `LIMIT`. `reencrypt_all` also materializes the whole credential table into memory in one transaction.
- **Impact:** A team with many models/keys or a large usage history makes a single list/dashboard/rotation call load unbounded rows → latency/OOM and long lock holds.
- **Fix:** Add pagination to list endpoints; batch `reencrypt_all` (paged commits).

### M6 — Usage-recording failure is silently swallowed
- **Where:** `application/completion_service.py:69-85`
- **Issue:** `_observe` wraps `usage.record` in `except Exception: logger.warning(...)` (fail-safe by design so billing never breaks a served request). But a DB hiccup means the request is served (and billed upstream) with **no usage row**, permanently under-counting.
- **Fix:** Keep fail-safe behavior but add a durable fallback (retry queue / dead-letter / metric+alert) so dropped billing events are recoverable and visible.

### M7 — `Model.update` cannot reset an optional field back to `None`
- **Where:** `application/model_service.py:88-93`
- **Issue:** `update` applies `{k: v for … if v is not None}`, so a previously-set optional field (`api_version`, `input_cost_per_token`, `output_cost_per_token`) can never be cleared to `null`. (`enabled=False` works — `False` is not `None`.)
- **Fix:** Distinguish "field omitted" from "field set to null" (e.g. a sentinel/`UNSET` marker or an explicit updatable-fields set).

### M8 — Vertex credential parsing can surface key material on error
- **Where:** `infrastructure/llm/vertex_adapter.py` (client build: `json.loads` + `service_account.from_service_account_info`)
- **Issue:** Malformed stored credential JSON raises `JSONDecodeError`/`ValueError` inside the request path with no translation; depending on error handling this can log/echo fragments of the service-account JSON (which contains the private key).
- **Fix:** Wrap credential parsing and raise a `CredentialMisconfigured` domain error with a non-revealing message; never include the raw value in the exception.

### M9 — Unauthenticated OpenAPI docs expose the full admin surface
- **Where:** `app.py:58-60, 113-127` (`OpenAPIConfig(path="/", render_plugins=[…])`)
- **Issue:** Swagger (`/`), Scalar, Stoplight, and `/openapi.json` are mounted publicly with no guard, disclosing the complete schema (credential, org/team-admin, invite endpoints).
- **Impact:** Schema disclosure aids targeted attacks. (Common to expose intentionally, hence MEDIUM.)
- **Fix:** Gate docs behind auth in production, or make exposure an explicit config toggle.

### M10 — Numeric env vars parsed with no validation
- **Where:** `config.py:105-108` (`int()`/`float()` on `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `REQUEST_TIMEOUT`, `MAX_RETRIES`)
- **Issue:** Bare `int()/float()` — a unit suffix (`60s`) or negative value crashes startup with an opaque `ValueError` (no field context) or is silently accepted (negative pool size).
- **Fix:** Parse with field-named errors and range validation (`> 0`).

### M11 — Inconsistent transaction boundaries in the persistence layer
- **Where:** `membership_repository.py` (only flushes, relies on service unit-of-work) vs every other repo (commits internally)
- **Issue:** Mixed conventions are a maintenance trap: a new caller of `membership_repository.add/remove` without a `_unit_of_work()` wrapper silently loses the write.
- **Fix:** Standardize — either all repos commit internally, or all defer to an explicit unit of work.

### M12 — Streaming SDK response not closed on client disconnect
- **Where:** `infrastructure/llm/openai_adapter.py:84-104`; consumers in `completions.py`
- **Issue:** The async generator yielding chunks doesn't close the underlying SDK/httpx stream if the client disconnects mid-stream.
- **Fix:** Use `try/finally` around the `async for` to `aclose()` the stream on generator finalization.

### M13 — Provider adapters and the gateway have no unit tests
- **Where:** `infrastructure/llm/gateway.py`, `openai_adapter.py`, `anthropic_adapter.py`, `vertex_adapter.py`, `responses_emulation.py`
- **Issue:** No `test_gateway`/adapter tests; only `request_policy` and `completions` are covered. Capability routing and request/response translation are untested.
- **Fix:** Add adapter tests with mocked SDK clients (translation shapes, capability dispatch, error mapping).

---

## LOW

- **L1 — Rate limit is per-IP via untrusted remote addr** (`infrastructure/web/rate_limit.py`). Behind a proxy without forwarded-header trust, all clients collapse to one IP (global throttle / attacker not distinguished). Documented in the module, but no startup assertion. *Fix: require proxy-header config or a trusted-proxy setting.*
- **L2 — Missing `tv` claim defaults to 0** (`infrastructure/web/session/jwt.py:40`). A validly-signed token lacking `tv` is treated as version 0 and accepted for a never-logged-out user. Narrow, needs a valid signature. *Fix: require `tv` or reject tokens without it.*
- **L3 — No admin-forced session revocation / account-disable** (`infrastructure/web/session/dependencies.py:37`). An admin cannot invalidate a compromised user's 7-day JWT; only the user's own `/logout` bumps `token_version`. *Fix: add an `is_active`/disabled flag checked at auth, and an admin revoke that bumps `token_version`.*
- **L4 — Request DTOs lack format/length validation** (`web/*/schemas.py`). `email`/`name`/etc. accept raw `str`; bad values surface as generic downstream errors instead of a 422, and unbounded strings reach the DB. *Fix: validated types / `max_length` on DTO fields.*
- **L5 — `DELETE` endpoints don't set explicit `204`** (`web/teams/controller.py` etc., vs `password_reset.py` which does). Relies on Litestar defaults; inconsistent status contract. *Fix: set `status_code=HTTP_204_NO_CONTENT` on delete handlers.*
- **L6 — `usage_event.model_name`/`operation` unindexed** (`persistence/orm.py:133-134`). `aggregate` filters/orders on `model_name` with no supporting index → seq scan as usage grows. *Fix: add a composite index for the aggregate query.*

---

## Reviewed and verified as NOT issues (or by design)

- **SQL injection** — none; all queries use SQLAlchemy Core/ORM with bound parameters, no `text()`/string interpolation.
- **JWT algorithm confusion** — not present; `Token.decode(..., algorithm="HS256")` pins the algorithm. Secret comparison is constant-time (`secrets.compare_digest`).
- **Tenant scoping for models & API keys** — enforced in the service layer (`ModelService._get_scoped`, `APIKeyService.revoke_for_team` check `team_id`). Repos are intentionally thin; this is a valid hexagonal split.
- **Credentials are a global, unscoped pool** (`domain/entities.py` `Credential` has no `team_id`/`organization_id`; `ModelService._validate_credential` checks only existence + provider) — **by design** (product decision). Provider credentials are managed centrally by cloud ops (platform-admin only, via `CredentialController`), and all organizations intentionally draw from the same pool; there is no per-org credential isolation to enforce. Since the encrypted secret is never returned and `api_base` is credential-fixed, a team can *use* a credential it references but never read it. Cost attribution is still per-team (recorded on `UsageEvent`).
  - *Optional future enhancement (nice-to-have, not required):* allow restricting a credential to one org via a **nullable** `organization_id` on `Credential` — `NULL` = global (default, current behavior), set = usable only by teams of that org. `_validate_credential` would then accept when `credential.organization_id is None or == team.organization_id`. Backward-compatible (existing credentials stay global) and opt-in.
- **SSRF via `api_base`** — mitigated by design: the endpoint comes only from the admin-managed credential, never the team-controlled model (`openai_adapter._base_url` comment).
- **SSO adopting an existing account by email** — intentional after the recent SSO hardening: adoption requires `email_verified` **and** an account not already bound to a different `sub`; role is re-synced from IdP groups (IdP as source of truth). This is the designed JIT-linking behavior, not a bug.
- **Response DTOs leaking secrets** — none; `UserResponse` exposes only `id/email/is_admin/created_at` (read-only), no `password_hash`/`token_version`/`key_hash`; credential `values` are never returned.
- **Sync gateway methods "blocking the event loop"** — the async web path uses only the `a*` methods; the sync methods are documented as library-only. Not a live defect (would only bite a future misuse).
