# Round 6 — New-feature deep review (2026-07-08)

[← Index](INDEX.md)

Sixth pass, run after Round 5's remediation and the large feature wave that landed since
(smart routing phases 1–5, SCIM provisioning, extended RBAC + platform auditor, SSO
group→team mapping, AWS Bedrock provider, per-provider credential validation — ~6,600 net
new lines across ~100 files). Six reviewers were pointed at the **new surfaces** rather than
the whole tree: smart-routing logic, SCIM security, Bedrock/credential-validation, RBAC/SSO
security, a whole-repo maintainability sweep, and a tests+architecture pass. Every finding
below was **re-verified against source** (each `file:line` read by hand; the two CRITICALs
and every HIGH re-confirmed directly), and cross-checked against Rounds 1–5 so nothing
already tracked is re-reported.

**Baseline is healthy:** the full suite is green (**557 passed**), `ruff` and `pyrefly` are
both clean, no file exceeds 800 lines, hexagonal import discipline still holds
(`domain/`+`application/` import neither `infrastructure` nor `litestar` — grep-verified),
and no secrets are committed (`.env`/`api_keys.db`/`.coverage` are all git-ignored). The core
security primitives from prior rounds remain intact (parameterized queries, pinned JWT alg,
constant-time/DB-hash key lookup, fresh-per-request RBAC checks with no TOCTOU vs live JWTs,
SDK clients closed in `finally`, credential secrets never returned).

**What Round 6 surfaces is a governance/data-exposure theme in the new features:** the
permission taxonomy and the SCIM admin-guard are both a notch too coarse (raw prompts and an
admin's login identity are reachable by roles that shouldn't have them), the routing
shadow-path shares a request-scoped DB session across concurrent tasks, the webhook strategy
is an unguarded SSRF + plaintext-secret sink, and the SSO privilege path has no audit trail.
None are open front doors to an anonymous attacker, but several are real escalation/exposure
paths reachable with a legitimately-issued low-privilege role or a misconfigured/compromised
IdP.

New counts: **2 CRITICAL · 6 HIGH · 12 MEDIUM · 5 LOW**.

## CRITICAL

### R6-C2 — Routing-decision export is gated on `usage:read`, leaking raw prompts to billing-only roles org-wide

- **Where:** `infrastructure/web/routing/controller.py:324` (gate) + `:333-343` (payload); `domain/authorization.py:40,47` (`BILLING_VIEWER` and `AUDITOR_TEAM_PERMISSIONS` both hold `USAGE_READ`); auditor cross-team bypass in `application/team_service.py:108-126`.
- **Issue:** `GET /teams/{id}/routers/{id}/decisions/export` dumps `{"text": r.user_text, "system_prompt": r.system_prompt, ...}` — the actual end-user prompt and system prompt — but is gated only on `Permission.USAGE_READ`, the same permission meant for token/cost aggregates. `TeamRole.BILLING_VIEWER` holds `USAGE_READ`, and `User.is_auditor` is granted `USAGE_READ` in **every team without membership**. So a "billing-only" viewer, and any platform auditor across **all** organizations, can exfiltrate raw prompt/system-prompt content (routinely PII, business-confidential text, pasted secrets). **Verified:** gate and payload read directly; `USAGE_READ` membership in both roles confirmed in `authorization.py`.
- **Fix:** Introduce a distinct `Permission.DECISIONS_READ` granted only to `ADMIN` (optionally `MODEL_MANAGER`), gate `export_decisions` on it, and exclude it from `AUDITOR_TEAM_PERMISSIONS`. Add an RBAC test asserting `billing-viewer` and auditor get 403 on `/decisions/export`.

#### R6-C3 — Shadow-mode routing reuses the request-scoped `AsyncSession` from a detached task (concurrent session use)

- **Where:** `application/routing/service.py:249-252` (`asyncio.create_task(self._run_shadow(...))`) → `_run_shadow` → `_build_strategy` for a `judge`/`embeddings` shadow strategy, which calls `self._models`/`self._credentials` — both built on the **request-scoped** `db_session` in `infrastructure/web/api_router/dependencies.py:73-80`. Only the shadow *decision log* gets its own session (via `shadow_decision_log_factory`); the strategy's own DB/credential lookups do not.
- **Issue:** The shadow task is fire-and-forget and outlives nothing — it races the primary request coroutine, which is still issuing statements (`_meter.settle_ok`, usage writes) on the **same** `AsyncSession`. SQLAlchemy `AsyncSession` is explicitly not safe for concurrent use across tasks. With `shadow_strategy: "judge"` or `"embeddings"`, this yields `IllegalStateChangeError`/`InterfaceError`, corrupted session state, or — after DI teardown returns the connection to the pool — reuse of a live connection by an unrelated request while the shadow task still writes. The exact bug class the code deliberately avoided for the decision log, but missed for the strategy lookups. **Verified:** task creation, `_run_shadow` lookups, and request-scoped repo wiring all read directly.
- **Fix:** Build the shadow strategy's `models`/`credentials` (and any DB lookup) from an independent session/session-maker, mirroring the care already taken for `_shadow_decisions`.

### HIGH

#### R6-H16 — SCIM admin protection guards only `active`, not the admin's `userName`/`externalId`

- **Where:** `application/scim_service.py:153-162` — `if user.is_admin and not target_active: raise PermissionDenied(...)` is the *only* admin guard; the subsequent `update_scim_identity` rewrites email/external_id with no admin check.
- **Issue:** The subsystem's stated invariant is "the gateway, not the IdP, governs admins," but a holder of the (single, shared) SCIM bearer token can `PATCH`/`PUT` a platform admin's `userName` (login email) or `externalId`. Renaming the admin's email locks them out of password login (login is keyed by email) and frees the original address for later JIT/SSO re-provisioning — an identity-integrity/lockout path the code claims to prevent. **Verified:** guard and identity-update path read directly.
- **Fix:** Extend the guard to reject `email`/`external_id` changes on `is_admin` accounts too; add the mirror of `test_scim_cannot_deactivate_platform_admin` for identity fields.

#### R6-H17 — Routing fallback bypasses the hard capability filter, breaking the "never fail the request" guarantee

- **Where:** `application/routing/service.py:322-340` — on strategy exception/timeout, `_run_strategy` returns `router.default_model` unconditionally, without checking it is a member of `capable` (the capability-filtered subset from `route()`).
- **Issue:** With ≥2 capable candidates and a strategy that raises (webhook down, judge malformed, timeout), the fallback can route to a `default_model` that was filtered out for lacking a required capability (e.g. vision). The request then hits a model that rejects the image → provider 400 — contradicting the documented invariant "capability filters run before any strategy; a strategy failure must never fail the request." **Verified:** fallback branch read directly.
- **Fix:** In the fallback, use `default_model` only if it's in `capable`; otherwise pick any `capable` member and record that `default_model` was skipped for capability.

#### R6-H18 — Webhook routing strategy has no SSRF protection on admin-configured URLs

- **Where:** `application/routing/webhook.py:38-40` (scheme-only validation) and `:57-58` (direct `client.post(self._url, ...)`).
- **Issue:** `WebhookStrategy` validates only `http(s)://`; no block on loopback/link-local/private ranges, no redirect disabling, no DNS-rebind guard. Anyone with `models:manage` can set `strategy_config.url` to `http://169.254.169.254/...` (cloud metadata) or an internal endpoint, and every routed request makes the gateway POST prompt content to it — and then *trusts the response's `choice`* to select a model. SSRF probe/exfil, or pivot into internal infra. **Verified:** constructor and request read directly.
- **Fix:** Resolve+validate the host against a private/loopback/link-local deny-list, disable redirect following, or route via an allow-listed egress proxy.

#### R6-H19 — Webhook `bearer_token` stored and returned in plaintext

- **Where:** `router_repository.py` persists `strategy_config` as raw JSON; `infrastructure/web/routing/controller.py:104-116` (`RouterResponse.from_entity`) echoes `strategy_config` verbatim on every create/update/list/get.
- **Issue:** The webhook secret lives inside `strategy_config` — stored unencrypted (unlike `CredentialRepository`, which envelope-encrypts) and returned unmasked by every read endpoint. A DB dump, backup, or logged response leaks it in cleartext. Today `MODELS_READ` always ships with `MODELS_MANAGE`, so it's not yet an inter-role leak, but it becomes an IDOR the moment a read-only model-viewer role exists, and it's inconsistent with the codebase's own secret posture. **Verified:** response mapper read directly.
- **Fix:** Encrypt `bearer_token` via the existing keyring/credential envelope, and redact secret fields from `RouterResponse`.

#### R6-H20 — SSO-driven admin/team-role changes are never audit-logged

- **Where:** `infrastructure/web/session/sso.py` — `sso_callback` has no `record_audit` import or call (grep-verified); contrast `users/set_admin.py:38-45`, `set_auditor.py:37-44`, `teams/controller.py` which audit every human-initiated equivalent.
- **Issue:** SSO can JIT-create a platform admin, upgrade an existing account to admin via IdP admin-group membership, and grant/revoke/change **team-admin** role via `reconcile_sso_memberships` — none audited. This is the escalation path an attacker would actually target (it needs only IdP group membership, not the admin API), and it leaves no trail for the very auditor role the system just added. **Verified:** absence of audit calls in `sso.py` confirmed by grep.
- **Fix:** `record_audit(...)` for JIT creation, admin upgrades, and each membership add/update/remove from reconciliation (actor_type `sso`/`system`); assert them in `GET /audit`.

#### R6-H21 — Bedrock mid-stream service errors bypass upstream-error translation → opaque 500

- **Where:** `infrastructure/llm/errors.py:63-76` (`_status_code` reads `exc.response["ResponseMetadata"]["HTTPStatusCode"]`), consumed from `bedrock_adapter.py:290,297` via `translate_stream`.
- **Issue:** Mid-stream Bedrock errors (`InternalServerException`, `ModelStreamErrorException`, `ValidationException`, `ServiceUnavailableException`) are raised by botocore as `EventStreamError`, whose `.response` has **no `ResponseMetadata`** — so `_status_code` returns `None`, `translate_upstream_error` returns `None` (only `ThrottlingException` is rescued by name), and the raw exception re-raises as an unmapped 500 mid-SSE. The same error on a *non-streaming* call maps correctly to 502/retryable. Client retry/backoff breaks and it looks like a gateway bug — the exact failure `errors.py` exists to prevent. **Verified as a logic gap** by reading `_status_code`/`translate_upstream_error`; the concrete botocore shape is library-behavior-based, and the existing test (`tests/completions/test_bedrock.py:185-209`) only exercises a synthetic non-streaming `ClientError`, so it's genuinely uncovered.
- **Fix:** Special-case `EventStreamError` (or absent `ResponseMetadata`) by mapping the AWS `Error.Code` to a status via a small table (server/unavailable/stream/validation + the existing throttle codes).

### MEDIUM

- **R6-M37 — SCIM PATCH `_set_attr` skips the type validation POST/PUT apply** (`web/scim/schemas.py:89-99`). `userName`/`externalId` are coerced with `str(value)` and no `isinstance` check (unlike `parse_user_payload`); a PATCH with `value: null` or an object persists `"none"`/`"{'k':'v'}"` as the user's real login email with a `200 OK`, silently destroying the identity. *Fix: validate `isinstance(value, str)` (non-empty for `userName`) → SCIM 400 `invalidValue`.*
- **R6-M38 — SCIM `PUT` defaults `active` to `True` when omitted** (`web/scim/schemas.py:63`). A PUT body that omits `active` silently reactivates a deactivated user as a side effect of an unrelated attribute sync. *Fix: preserve current value on omission, or reject PUT bodies missing `active`.*
- **R6-M39 — SCIM can silently resurrect an admin-disabled account** (`application/scim_service.py:140-176`). `is_active` is one shared boolean; nothing distinguishes "disabled by an admin for cause" from "IdP-deprovisioned," so a routine IdP full-sync with `active: true` re-enables an account an admin just disabled, and it logs only as generic `scim.user.update`. *Fix: track `deactivated_by` and no-op/reject SCIM reactivation of admin-disabled accounts; at minimum emit a distinct `scim.user.reactivate` audit action.*
- **R6-M40 — Estimated-savings SUM silently drops rows with NULL `completion_tokens` it still counts** (`persistence/router_repository.py:190-216`). `counted` requires only `prompt_tokens`+input costs non-null, but the SUM multiplies by `completion_tokens` un-coalesced, so a row with `completion_tokens IS NULL` contributes `NULL` (dropped from the aggregate) while `counted_n` still includes it — savings understated with no `decisions_without_usage` signal. Coalescing only one side's output cost to `0.0` also skews asymmetrically. **Verified:** SQL read directly. *Fix: require `completion_tokens` and both output costs non-null (or all null) in `counted`.*
- **R6-M41 — "alt" (most-expensive-candidate) selection excludes candidates missing `input_cost_per_token`** (`application/routing/service.py:382-401`). A candidate with `input_cost_per_token=None` but a high `output_cost_per_token` is dropped from the priced set, understating the counterfactual cost and thus reported savings. *Fix: treat a candidate as priced if either cost field is set.*
- **R6-M42 — Embeddings-routing caches are unbounded, never-evicted process globals** (`application/routing/embeddings.py:35-36,105-121`). `_ROUTE_CACHE`/`_CACHE_LOCKS` are keyed by a hash of the full routes config; every config edit adds a new key and old entries/locks live for the process lifetime → unbounded memory growth across edits. *Fix: LRU bound, or key by router id so edits replace.*
- **R6-M43 — `/keys/spending` exposes the API-key inventory under `usage:read`, bypassing `keys:read`** (`web/teams/controller.py:194-218`). `list_keys` requires `KEYS_READ`, but `keys_spending` returns effectively the same inventory (id/name/prefix/is_active/timestamps) gated only on `USAGE_READ`, so `BILLING_VIEWER` and cross-team auditors enumerate key names/prefixes/lifecycle without `KEYS_READ`. *Fix: require `KEYS_READ` for the identity block, or document the widened billing-viewer scope explicitly.*
- **R6-M44 — Non-deterministic SSO role resolution on multi-group same-team mappings** (`web/session/sso.py:49-61`). When two IdP groups map the same team to two different non-admin roles, whichever appears first in the IdP-ordered `groups` claim wins (ADMIN correctly always wins; non-admin ties don't). Role selection then varies with IdP claim ordering, silently diverging from operator intent. *Fix: reject same-team multi-role conflicts at config load, or sort `groups` deterministically and define a total precedence.*
- **R6-M45 — Bedrock does one `asyncio.to_thread` hop per stream event on the shared default pool** (`bedrock_adapter.py:47-50,288-296`). Correct for one request, but a handful of concurrent Bedrock streams issuing many tiny thread hops can saturate the process-wide default `ThreadPoolExecutor`, adding latency to unrelated requests app-wide. *Fix: dedicated bounded executor for Bedrock blocking calls, or batch event pulls per hop.*
- **R6-M46 — Bedrock Titan embeddings issue one sequential network call per input** (`bedrock_adapter.py:326-343`). `for text in texts: _invoke(...)` runs N round trips sequentially inside one thread hop (Titan has no batch endpoint), so latency scales linearly with batch size with no fan-out. *Fix: gather the per-text calls across a small pool, or document the linear-latency behavior.*
- **R6-M47 — `create_app()` is a 175-line composition-root god-function with two nested closures** (`app.py:78-253`). Observability wiring, two nested `Provide` factories, route list, dependency map, conditional SSO, OpenAPI, and rate-limit store selection are all inline. Touched on every feature add; nested closures are hard to unit-test. *Fix: extract `_build_dependencies`/`_build_route_handlers`/`_build_openapi_config`/`_build_rate_limit_stores` module-level helpers.*
- **R6-M48 — `UsageMeter.metered_stream` is the only function past nesting depth 4** (`application/usage_meter.py:362-434`, ~125 lines, depth 5). Correctness-critical shielded billing/tracing is bundled with stream relaying and cancellation plumbing in one `try/except/finally`. *Fix: extract the finally-block post-processing into `_finalize_stream_billing(...)`.*

### LOW

- **R6-L30 — `resilience.py` (timeout/retry wiring) has zero test coverage** (`infrastructure/llm/resilience.py`; grep: no `ResilienceConfig`/`resilience` references under `tests/`). The mechanism that stops a slow upstream from hanging the gateway is unverified — a refactor could silently drop it and only fail in production. *Fix: per-adapter test asserting `resilience.client_kwargs`/`timeout_ms` reach the SDK client constructor (fakes already capture init kwargs).*
- **R6-L31 — Money math is `float` end-to-end with no property-based tests** (`domain/entities/model.py:42`, `application/usage_meter.py:113-129`). Budget-gate tests use exactly-representable literals, so float-summation drift at the enforcement margin is never exercised. Extends the known L15; flag if invoice-grade accounting becomes a goal. *Fix: `Decimal`/integer micro-USD + Hypothesis round-trip/associativity tests.*
- **R6-L32 — Test doubles capture call args on class attributes, not instances** (`tests/completions/test_bedrock.py:227-228`, `tests/completions/conftest.py:76-77,153-154,308-309`, `tests/routing/test_webhook_shadow.py:58`, `test_judge_hybrid_export.py:56`). Safe only under sequential execution; breaks silently under `pytest-xdist` and can mask a regression by reading stale state. *Fix: store on `self` with a fresh fake per test, or reset via an autouse fixture.*
- **R6-L33 — Dead `keys_match` + stale docstring in `key_generator.py`** (`domain/key_generator.py:4,30-31`). `keys_match` (constant-time compare) is never called — real verification is DB hash-equality lookup — yet the module docstring claims constant-time comparison is used. Misleading in a security-sensitive module. *Fix: delete the unused function and correct the docstring.*
- **R6-L34 — Persistence/identity/lock ports each have exactly one adapter** (`infrastructure/persistence/*`, `sso/oidc.py`, `locks.py`). The ports layer's proven value here is enabling fast in-memory test doubles, not swappable backends (`LLMGateway` with 5 provider adapters is the genuine payoff). Not a defect — noted to temper "add a port for hexagonal purity" pressure. *Fix: none; optionally document ports as a testability seam.*

### Category scores (this round)

| Category | Score | One-line justification |
|---|---|---|
| Code quality / maintainability | **8/10** | Clean linters, no >800-line files, no print/TODO/mutable-defaults; only `create_app` and `metered_stream` hot spots + uneven port docstrings remain. |
| Architecture | **8/10** | Hexagonal boundaries enforced in practice (grep-clean), correct dependency direction; ports mostly buy testability not extensibility. |
| Tests | **7.5/10** | 557 green with real behavioral assertions on risky paths (reservation races, Bedrock translation, tenant 403s), but float money math, class-attr fake state, and an entirely untested resilience module pull it down. |
| Security — RBAC/SSO | **6/10** | Solid, TOCTOU-free permission model, but `usage:read` over-exposure (C2) + no SSO audit trail (H20) trade data-minimization/detectability for convenience. |
| Security — SCIM | **5/10** | Good token/entropy/revocation mechanics, but the "gateway governs admins" promise is half-enforced (H16) and PATCH skips type validation (M37). |
| Logic correctness — smart routing | **6.5/10** | Clean strategy model, but a real concurrency defect (C3), a capability-filter fallback gap (H17), and several savings-math edge cases reachable with realistic configs. |
| LLM adapters (Bedrock/Azure/credential-validation) | **6.5/10** | Careful translation + correct sync-boto3-off-loop design, capped by the streaming error-translation regression (H21) and shared-pool/sequential-embed performance gaps. |

**Overall (this round): 6.5/10** — a well-engineered, thoroughly-tested codebase whose *new*
feature wave outran its governance model: the recurring theme is coarse permissions and
half-complete guards exposing sensitive data (prompts, admin identity, secrets) to roles or
paths that shouldn't reach them, plus one genuine concurrency bug in the routing shadow path.
All findings are fixable with targeted guards/permission splits rather than redesign; none
block the existing happy paths, which is why the suite stays green. Fixing C2, C3, and the six
HIGHs would bring this to a defensible 8+.
