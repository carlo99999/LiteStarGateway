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

## Round 5 — Graph-guided full-project review (2026-07-06)

Fifth whole-codebase pass, run immediately after Round 4's M30/L23 + M32 remediation
(PRs #106, #108). This round was **graph-guided**: the repo was first turned into a
knowledge graph (`graphify`, 2,385 nodes / 6,583 edges / 138 communities), and six
parallel reviewers were each pointed at one community cluster — budget/usage/completion,
web auth, user lifecycle, SSO/crypto/keyring, LLM gateway/adapters, and
controllers/persistence/config/observability. Every finding below was **re-verified
against source** (each `file:line` was read, and the HIGH was re-confirmed by hand),
and cross-checked against Rounds 1–4 so nothing already tracked is re-reported.

The headline: Round 4's two most recent fixes were independently verified **correct and
complete** — commit `3052346` (`_reservation_cost` now scales the output term by a
type-safe `n`, and all six completion paths call `sanitize_request` before `_prepare`,
closing M30 and L23; new regression tests pass). The architecture again held up
(hexagonal boundaries clean, no IDOR across the team/org/model/SP controllers, JWT
`token_version`+`is_active` checked per request, enumeration-resistant login, atomic
single-use token redemption, SDK clients closed in `finally`). What Round 5 surfaces is
one **inverted precedence** in the adapter layer that lets clients override trusted admin
policy, plus a rate-limiter bypass, a pagination regression in the last-admin invariant,
and two audit/observability gaps.

New counts: **0 CRITICAL · 1 HIGH · 4 MEDIUM · 3 LOW**.

### Resolution status (Round 5)

**Fixed (this pass):**

| Finding | Fix |
|---|---|
| H15 — `model.params` precedence conflated three governance semantics | `Model.params` (client-overridable defaults) + new `Model.params_enforced` (admin policy, applied last) via `Model.merge_params`, used at all five adapter sites; new per-model `Model.max_output_tokens` ceiling clamped with `min` and injected on omission inside `clamp_output_tokens`, applied in `_prepare` before the reservation so admission and the provider call agree. Migration `b3c4d5e6f7a8` adds both columns (nullable → existing models unchanged). This also delivers the Round 4 proposed `max_output_tokens` follow-up. Regression tests: `tests/test_model_params.py`, `clamp_output_tokens` cases in `tests/test_request_policy.py`, and four end-to-end cases in `tests/test_completions.py`. Full suite green (341). |

**Fixed (remediation pass — all remaining Round 5 findings):**

| Finding | Fix |
|---|---|
| M33 — inference rate-limiter bypassable by varying the bearer token | `_inference_identifier` now keys by client IP (the one thing an unauthenticated caller can't cheaply vary); per-client spend stays bounded by the budget gate. |
| M34 — last-admin check truncated to the first page of memberships | New `TeamMembershipRepository.count_admins`; `_is_last_admin` checks the target is an admin + the admin count is 1 (unpaginated). |
| M35 — admin invite / password-reset issuance not audited | `create_invite` and `create_password_reset` now emit `invite.create` / `password_reset.create` via `record_audit`. |
| M36 — OIDC `groups` claim assumed to be a list | `_parse_groups` accepts only a list/tuple (absent = no groups); fails closed on any other shape. |
| L27 — OIDC discovery/JWKS cached with no TTL | Discovery refreshed after a 1h TTL, serving the cached copy if a refresh fails. |
| L28 — SSO flow cookies not cleared on callback | `sso_callback` expires `sso_state`/`sso_nonce`/`sso_verifier` (max-age=0). |
| L29 — `TeamRepository.list_by_organization` unbounded | Added `limit`/`offset` matching the sibling repos (preventive; still no caller). |

Each fix has a regression test; the full suite stays green. **Round 5 is fully
remediated.**

### HIGH (Round 5)

#### H15 — `model.params` precedence is a single dumb dict-merge, conflating three distinct governance semantics

- **Where:** `infrastructure/llm/openai_adapter.py:34` (`merged = {**model.params, **request}`); identically `anthropic_adapter.py:45` and `vertex_adapter.py:52,121,144`. The inline comment (`openai_adapter.py:33` — "Model params are defaults; the incoming request overrides them") **contradicts** `docs/param-allowlist.md:43-44` ("`model.params` … is applied **after** the sanitized client request, so admin settings can't be overridden by clients"): code and design doc disagree on the intended precedence, so neither can be trusted as the spec.
- **Issue:** A flat `{**model.params, **request}` (client wins) *and* its naive inverse `{**request, **model.params}` (admin wins) are **both** wrong, because different params want different governance. Three cases:
  1. **Generation knobs** (`temperature`, `top_p`, `presence_penalty`, `frequency_penalty`, `seed`): legitimately per-request — the client should tune them, with `model.params` acting as a *default*. Client-wins is correct here.
  2. **Cost ceilings** (`max_tokens`/`max_completion_tokens`/`max_output_tokens`, `n`): want **`min` (clamp) semantics, not substitution**. The client picks a value, the system lowers it to the admin ceiling; the client may ask for *less*, never *more*. A dict-merge in either direction is wrong — admin-wins caps (and over-reserves for) a client who asked for less; client-wins lets the ceiling be ignored. Worse: `model.params` is **not** run through `sanitize_request`'s clamp (`request_policy.py:84` — "trusted admin params are not capped"), so an admin-wins flip would let a `max_tokens` sitting in params bypass the global 32k ceiling **and** desync from the budget reservation (computed from the *sanitized* request) → a cost/budget bypass that re-opens the class of bug M30/L23 just closed.
  3. **Hard policy** (a `response_format` schema a downstream parser requires, a mandatory `stop`, a locked `tool_choice`): admin must win (substitution). Frequently empty.
- **Fix:** Model the three semantics explicitly instead of picking one merge direction:
  - Split `Model.params` into `defaults` (client overrides) and `enforced` (admin wins), building the effective request as `{**model.params_defaults, **client_request, **model.params_enforced}` at all five adapter sites (`model` is already force-set to `provider_model_id` downstream, so it is unaffected).
  - Add the per-model `max_output_tokens` ceiling (the Round 4 follow-up) and apply it as a `min` clamp **inside `sanitize_request`** (`ceiling = model.max_output_tokens or MAX_TOKENS`), so the clamp runs *before* the reservation and provider + reservation see the same number.
  - Keep the token/`n` fields out of the `enforced` bucket (or re-clamp `effective` after the merge) so no admin value can re-inflate a cost ceiling.
  - Regression tests: (a) a client-set `temperature` overrides the model default; (b) an `enforced` param cannot be overridden by the client; (c) `max_output_tokens` clamps with `min` and stays in sync with the reservation. Update the `openai_adapter.py:33` comment and `docs/param-allowlist.md` to match. **Which field lands in `defaults` vs `enforced` is the admin's per-field choice — only the cost-ceiling category has fixed `min` semantics because it is tied to budget.**

### MEDIUM (Round 5)

#### M33 — Inference rate limiter is bypassable with garbage tokens → pre-auth DB-load DoS

- **Where:** `infrastructure/web/rate_limit.py:31-42` (`_inference_identifier` keys the bucket by `sha256(bearer_token)`) + `infrastructure/web/api_router/router.py:29-39` (limiter runs *before* `APIKeyAuthMiddleware`); each request still reaches `auth.py:40-54`, which opens a DB session and runs `APIKeyService.authenticate` → `repo.get_by_hash(...)`.
- **Issue:** Because the bucket key is the hash of the *unauthenticated* token, every distinct bearer value — valid or random garbage — gets its own independent 120/min bucket. An attacker sends `/v1/chat/completions` requests each with a fresh random `Authorization: Bearer <garbage>`; no two share a bucket, so the limiter never throttles the flood, and every request still costs a DB lookup. This defeats the module's own stated purpose ("bounds invalid-token floods") for any request carrying *some* bearer value, turning the auth path into a zero-cost DB-load DoS lever. (Distinct from L1's per-IP proxy-collapse and H9's forwarded-IP trust — this is about keying on an attacker-controlled token before authentication.)
- **Fix:** Key the pre-auth inference limiter by client IP (or a single shared unauthenticated bucket), and only widen to a per-key/per-team bucket after `APIKeyAuthMiddleware` has actually resolved the key — i.e. authenticate first, then rate-limit on the resolved principal. Alternatively add a cheap pre-auth format/entropy gate so malformed tokens can't mint fresh buckets.

#### M34 — Last-admin protection is truncated to the first 100 memberships (regression from the M5/#57 pagination fix)

- **Where:** `application/team_service.py:90` (`_is_last_admin` calls `self._memberships.list_by_team(team_id)` with no `limit`/`offset`) → `infrastructure/persistence/membership_repository.py:41-51` (`list_by_team` defaults to `limit=DEFAULT_PAGE_SIZE=100`, ordered by `created_at`).
- **Issue:** The M5/#57 fix added `limit`/`offset` to `list_by_team` but did not audit this internal invariant caller. On a team with >100 memberships, `_is_last_admin` inspects only the oldest 100 rows. If the team's remaining admin(s) were added *after* the 100th membership (e.g. the founding admin left and a later member was promoted), the fetched page contains zero admins, `_is_last_admin` returns `False`, and `set_role`/`remove_member` (`team_service.py:184-211`) will happily demote or remove the team's actual last admin — leaving the team adminless and requiring platform-admin recovery. Invisible to existing tests, which use small teams. This partially re-opens the original H1 (last-admin protection, #49) for large teams.
- **Fix:** Make `_is_last_admin` do a scoped count rather than page-and-scan — add a `count_admins(team_id)` repository method (or an admin-existence check) that isn't subject to the page-size default, and treat "exactly one admin" as the guard.

#### M35 — Admin-issued invites and password-reset tokens are not audit-logged

- **Where:** `infrastructure/web/users/invites.py:21-25` (`create_invite`) and `infrastructure/web/users/password_reset.py:33-39` (`create_password_reset`) — neither calls `record_audit(...)`, unlike sibling admin actions `unlock.py:32-39` and `set_active.py:34-41` which do.
- **Issue:** These two platform-admin-only endpoints mint the exact credentials capable of creating a new account or taking over an existing one, yet leave no audit trail. A compromised or malicious admin JWT can mint a victim's password-reset token (or an invite for a rogue account) and exfiltrate it out-of-band; an incident responder reviewing the audit log sees `user.enable`/`user.disable`/`user.unlock` for other admin actions but finds **zero** record that a reset token or invite was ever issued, by whom, or when — precisely the primitive needed to detect and attribute account-takeover-via-admin-abuse. (Distinct from M20, which concerns audit-write transactionality and missing login/SSO/denied events, not invite/reset issuance.)
- **Fix:** Call `record_audit(audit_log, request, admin_user, "invite.create", target_type="invite", target_id=issued.invite.id)` and `record_audit(..., "password_reset.create", target_type="user", target_id=user.id)` immediately after issuance in both handlers, mirroring `unlock.py`/`set_active.py`.

#### M36 — OIDC `groups` claim is assumed to be a JSON array; a string claim is iterated character-by-character

- **Where:** `infrastructure/sso/oidc.py:126,131` (`tuple(str(g) for g in groups)` with no type check on `claims.get("groups")`).
- **Issue:** Some IdPs emit `groups` as a single space/comma-delimited string rather than an array. If an IdP returns `"groups": "admins"`, the comprehension iterates the *string* into `("a","d","m","i","n","s")`, silently corrupting group→role mapping for every login from that IdP — usually under-privileging (locking legitimate admins out), but a pathological single-character admin-group name could cause over-granting to any claim containing that character. Not exploitable against the shipped test fixtures (which only use lists), but it is an unguarded parsing assumption on federated-identity-derived authorization data.
- **Fix:** Validate `claims.get("groups")` is a `list`/`tuple` before iterating; fail closed (raise `SSOExchangeError`) if it is a string or other type, rather than silently iterating it.

### LOW (Round 5)

- **L27 — OIDC discovery/JWKS cached for the provider's lifetime with no TTL** (`infrastructure/sso/oidc.py:70,141-142`). `_metadata` (endpoints/issuer) and `_jwks` are cached indefinitely; only an "unknown kid" miss triggers a reactive JWKS refetch. If the IdP rotates its `authorization_endpoint`/`token_endpoint`/`issuer` (migration, or a `jwks_uri` change for reasons other than routine kid rotation), the gateway uses stale values until process restart — an availability/operability gap, and a stale `issuer` could mask a legitimate IdP-side incident. *Fix: add a periodic TTL refresh (e.g. hourly) of the discovery document, independent of the kid-miss JWKS refetch.*
- **L28 — SSO flow cookies not cleared on callback** (`infrastructure/web/session/sso.py:99-119`). `sso_state`, `sso_nonce`, and `sso_verifier` are read but never explicitly expired (`max_age=0`) on success or failure; they linger until the 600s TTL. Low value in practice (`code`/`state` are still required and the code is single-use at the IdP), but a leftover flow cookie on a shared/kiosk browser stays replayable for the rest of the window. *Fix: clear all three flow cookies in the `sso_callback` response regardless of outcome.*
- **L29 — `TeamRepository.list_by_organization` has no `limit`/`offset`** (`infrastructure/persistence/team_repository.py:30-36`; port at `domain/ports.py:177`), unlike every sibling repository. Currently dead code (no controller calls it — grep-verified), so not exploitable today, but it's a landmine: the first "list teams in an org" feature wired into `OrganizationController` silently reintroduces the exact unbounded-query pattern M5/M17 fixed elsewhere. *Fix: add `limit`/`offset` now to match the other repositories, or remove the unused port method until a caller needs it.*

### Verified clean (Round 5 — checked specifically, no issue)

- **Commit `3052346` (M30/L23 fix)** — re-verified correct and complete by two reviewers: `_reservation_cost` (`usage_meter.py:110-121`) multiplies the output term by a type-safe `n` (rejects bool/non-positive, defaults to 1); all six paths in `completion_service.py:99-183` call `sanitize_request` before `_prepare`, so the reservation uses the clamped `n` (≤`MAX_N=8`) and clamped token ceilings (≤`MAX_TOKENS=32_000`). New tests cover per-choice reservation and the clamped-max-tokens path; full suite green.
- **No IDOR across controllers** — every team/model/service-principal/organization/audit endpoint scopes to `team_id` via `ensure_(principal_)can_manage_team`/`_get_scoped`/`_get_in_team` before any read or mutation; a UUID path param for another team's resource returns `…NotFound`, never cross-tenant data.
- **Auth stack** — HS256 pinned in both `Token.decode` calls (`session/jwt.py:37`); `token_version` + `is_active` checked every request (`session/dependencies.py:17-45`, logout invalidates all outstanding tokens); realm separation enforced twice (inference keys rejected on management endpoints and vice versa); API-key lookup is exact SHA-256 hash (256-bit random tokens, no meaningful timing channel); auth-endpoint (login) rate limiting is correctly per-IP.
- **User lifecycle** — enumeration-resistant login burns an identical Argon2 decoy hash on unknown-email/locked/wrong-password branches; invite/reset tokens are `secrets.token_urlsafe(32)`; both `mark_used` paths use conditional `UPDATE … WHERE used_at IS NULL` (no double-redemption race); `set_password` and `set_active(False)` both bump `token_version` in the same statement.
- **SSO/crypto core** — PKCE (S256) + nonce + state all generated, cookie-bound, and verified; `id_token` verification pins `iss`/`aud`/algorithm allowlist with no `alg=none`/HS256-confusion path; envelope encryption uses per-record key IDs with non-destructive retirement (rotation races degrade to redundant re-encryption, not data loss); Redis lock fails closed.
- **LLM gateway** — the request-policy allowlist (`domain/request_policy.py:20-110`) correctly excludes all transport/SDK-special kwargs (`extra_headers`, `extra_body`, `extra_query`, `timeout`, `api_key`, `base_url`); clamping is applied after filtering, non-mutating, and bool-safe; error translation (`llm/errors.py`) maps by status code only and never echoes provider response bodies; `translate_stream` re-raises `BaseException`/`CancelledError` untouched; all five adapters close their SDK clients in `finally` including on streaming paths.
- **Controllers/persistence** — `pagination.py` (`resolve_page`) applied consistently across all list endpoints; `exception_handlers.py` maps domain errors to status codes with generic details (no stack traces); every privileged mutation (member/role, key issue/revoke, budget set/remove, model CRUD, org/team create, SP lifecycle) writes an audit event *except* the two in M35; `config.py` fails fast in production on Postgres-required and weak/default `JWT_SECRET`/`SALT_KEY`/`MASTER_KEY`; no raw `text()` / SQL injection, no N+1 patterns; spot-checked migrations have symmetric, non-destructive-beyond-intent downgrades. <!-- pragma: allowlist secret -->
- **Re-confirmed still-open (not re-reported as new):** M27 (un-iterated stream generator reservation leak) and M28 (quarantined outbox rows still summed into the budget gate) remain live in the current code, exactly as documented in Round 4.

---

## Round 4 — Full-project review (2026-07-06)

Fourth whole-codebase pass, three days after Round 3's remediation (PRs #91–#103 all merged).
Run by four parallel reviewers (security, Python/async, architecture, adversarial billing);
every finding below was **re-verified against source** before inclusion — including reading the
actual `finally`/shield structure, the adapter generators, and the outbox SQL. Baseline:
**323 tests pass**, `ruff` and `pyrefly` clean, working tree clean, no tracked secrets
(`.env`, `api_keys.db`, `.coverage` all untracked).

The honest headline: the architecture held up (hexagonal boundaries grep-verified clean again,
ports still coherent, Round-2/3 operational fixes all genuinely in place, not papered over).
What Round 4 surfaces is concentrated on the **edges of the new money machinery** — the
in-flight reservation and the H13 settlement shield each have unhandled corner cases, one
provider adapter silently gives away free inference, and the streaming error-path billing
introduced by #103 over-bills in the one case it didn't consider.

New counts: **0 CRITICAL · 1 HIGH · 7 MEDIUM · 8 LOW**.

### Resolution status (Round 4, updated after remediation)

**Fixed & merged to `main`:**

| Finding | Fix PR |
|---|---|
| M32 — `UsageMeter` extracted from `CompletionService` | #106 |
| M30 + L23 — reservation scales with `n`; computed from the sanitized request | #108 |
| H14 — Vertex embeddings billed from reported/estimated usage (non-stream estimation fallback) | #113 |
| M26 — streams the provider rejects before any output bill nothing | #114 |
| M27 — stream reservation released even if the generator is never iterated | #115 |
| M28 — quarantined outbox rows excluded from the budget gate | #116 |
| M29 — shielded stream settlement bounded by a timeout | #117 |
| M31 — `OIDC_REDIRECT_URI` required when SSO enabled outside local dev | #121 |
| L19 — stream usage estimate sums all choices, not just `choices[0]` | #122 |
| L20 — `lockout_cycles` incremented in-database (atomic) | #123 |
| L21 — unauthenticated MLflow port no longer published to the host | #124 |
| L22 — image-generation billing gap documented in `docs/usage-cost.md` | #125 |
| L24 — dead `revoke_personal_keys_for_user` wrapper removed; annotation restored | #126 |
| L26 — stale README Postgres note dropped; default DB renamed `gateway.db` | #127 |

The proposed per-model `max_output_tokens` follow-up shipped as part of **H15**
(Round 5, #109): clamp with `min` semantics + inject on omission.

**Deferred (deliberate — not a defect):**

- **L25** — `TeamController` mixes members/keys/budgets/usage in ~340 lines. Each
  handler is individually fine; per the finding this is an early-warning to
  **split when it next grows** (into `BudgetController`/`UsageController`), not a
  bug to refactor now. Left as-is to avoid churning working routing/DI wiring.

### HIGH (Round 4)

#### H14 — Vertex/Gemini embeddings are always billed as zero tokens and zero cost

- **Where:** `infrastructure/llm/vertex_adapter.py:123-139` (`from_gemini_embeddings` hardcodes `usage={"prompt_tokens": None, "total_tokens": None}`); consumed by `application/completion_service.py:100-117` (`_parse_usage`: `int(None or 0) = 0`). The embeddings path goes through `_dispatch`/`_observe`, which — unlike the streaming path — has **no estimation fallback**.
- **Issue:** Every `/v1/embeddings` call against a Vertex-backed model records a `UsageEvent` with 0 tokens and 0.0 cost, regardless of input size. Arbitrarily large embedding batches are free, invisible to the budget gate, and under-reported in every aggregate. Deliberately exploitable by routing embeddings traffic at a Vertex alias. OpenAI/Azure/Databricks embeddings are unaffected (their SDKs return `usage.prompt_tokens`).
- **Fix:** Populate usage from the google-genai response's usage metadata where available; otherwise estimate `prompt_tokens` from the input text (`_estimate_tokens` already exists). Belt-and-braces: give `_observe` a non-streaming estimation fallback so an all-`None` usage block never bills zero silently. Regression test: Vertex embeddings call asserts a non-zero `UsageEvent`.

### MEDIUM (Round 4)

#### M26 — Streams the provider rejects upfront (429/401/400) bill the customer the estimated prompt for zero upstream consumption

- **Where:** `application/completion_service.py:461-500` (the shielded `finally` in `_metered_stream`, docstring "the provider consumed the prompt — bill it"); provider call is lazy — it runs inside the adapter generator on first `__anext__` (`openai_adapter.py:121`), i.e. *inside* `_metered_stream`'s `async for`. Contrast `_dispatch` (`:309-334`): the non-streaming path bills nothing on a provider exception.
- **Issue:** A pre-processing rejection (rate limit, bad gateway credential, invalid param) raises before any chunk: `streamed_chars == 0`, no authoritative usage. The `finally` builds an estimate whose `prompt_tokens > 0`, and because `error is not None` it **bills it** — charging the team for a request the provider rejected and charged the gateway nothing for. The docstring's assumption ("failure before the first content chunk — the provider consumed the prompt") is false precisely for the most common streaming failure mode. Asymmetric with the non-stream path (same error, zero bill), and it's the over-billing mirror of the deliberate L11/#103 under-billing fix — introduced by that fix and not covered by its tests (`test_provider_error_mid_stream_bills_streamed_usage` only streams chunks first).
- **Fix:** On the error path, bill only when something was produced: if `error is not None and streamed_chars == 0 and not _has_tokens(usage)`, emit the error trace with zeroed usage and skip `_bill`. Regression test: gateway raising on first `__anext__` → no `UsageEvent`.

#### M27 — In-flight budget reservation leaks when a stream generator is returned but never iterated

- **Where:** Reservation added in `_enforce_budget` (`completion_service.py:362-363`, called from `_prepare` before the generator exists); released **only** in `_metered_stream`'s `finally` (`:461`). `open_chat_stream`/`open_responses_stream` (`:517-548`) return the generator to the SSE controller.
- **Issue:** Python never runs the body — or the `finally` — of an async generator that is never started: `aclose()`/GC on an un-iterated generator is a no-op. If Litestar never begins the SSE body (client drops between handler return and first byte, or response setup raises), the reservation is leaked into the per-replica `InFlightSpend` forever. Leaked reservations accumulate until `spent + reserved >= limit` is permanently true and **every** request from that team gets a false 402 until the replica restarts. Revenue-safe (over-counts), but a slow availability failure; no test drops an un-iterated generator.
- **Fix:** Don't leave release solely to the generator `finally`: take the reservation on the generator's first step, or wrap the returned iterator so the controller's teardown releases it, or add a TTL sweep to `InFlightSpend`. Regression test: call `open_chat_stream`, drop the result, assert `in_flight.total(team_id) == 0`.

#### M28 — Quarantined (poison) outbox rows permanently inflate the budget gate for the rest of the window

- **Where:** `usage_repository.py:116-137` (`spend_since` sums **all** `pending_usage_event` rows, no `attempts` filter) vs `:163` (`reconcile_pending` skips rows with `attempts >= MAX_RECONCILE_ATTEMPTS`); quarantined rows are never deleted (kept "for inspection", module comment).
- **Issue:** The M21 and M23 fixes interact: a poisoned row (e.g. its team/key FK target was deleted) quarantines after ~10 cycles but keeps counting as live spend in every `_enforce_budget` call — it will never settle, never land in the ledger, and never stop being summed until the budget window rolls over (up to a month for `MONTHLY`). Phantom spend silently shrinks the team's usable budget by the poisoned amount. This is the deliberate-tradeoff flip side of M21 (which fixed under-counting); the tradeoff is currently undocumented and untested (`test_poisoned_row_cannot_starve_newer_events` uses distinct team ids, so it never observes `spend_since` post-quarantine).
- **Fix:** Decide the semantics explicitly: exclude quarantined rows from `spend_since` (a never-billable event stops gating), or archive quarantined rows out of the live table. Either way, document it in `docs/usage-cost.md` next to the M21/M23 notes and add a regression test for `spend_since` after quarantine.

#### M29 — Shielded stream settlement has no timeout: a stalled DB turns disconnect cleanup into unbounded orphan tasks

- **Where:** `completion_service.py:466` (`with anyio.CancelScope(shield=True):` around `_bill`/`_observe`); no `anyio.fail_after` inside the shield, and no statement/pool/command timeout configured anywhere (`infrastructure/persistence/database.py`, `config.py`).
- **Issue:** The H13 shield is correct and necessary, but unbounded: the settlement is a live DB write that nothing can cancel (shield) and nothing times out (no inner deadline). Under a real DB degradation, every disconnecting stream leaves an immortal cleanup coroutine holding a pool connection — the more the DB struggles, the more shielded writers pile up, competing with the reconciler and fresh requests for the same pool, and graceful shutdown can hang on them. Found independently by two reviewers.
- **Fix:** Bound the shielded block with `anyio.move_on_after(...)` (falling through to the existing outbox dead-letter path, which exists for exactly this "DB write failed" case) and/or set an explicit engine-level statement/acquire timeout.

#### M30 — In-flight reservation ignores `n`: the documented burst-overshoot bound is understated up to 8×

- **Where:** `completion_service.py:119-127` (`_reservation_cost`: prompt estimate + `max_output_tokens`, no `n` multiplier); `n` is client-controllable and allowlisted up to `MAX_N = 8` for chat and images (`domain/request_policy.py:30,75,85,105-106`).
- **Issue:** A `n=8` request generates up to 8 completions — post-hoc billing is correct (provider sums usage across choices), but the admission-time reservation covers only one choice, so the real committed cost can be ~8× the reservation. The honest M24 bound documented in `docs/usage-cost.md` ("N × per-request cost") is actually "N × n × cost", invisible in code, tests, or docs.
- **Fix:** Multiply the output term by `request.get("n", 1)` in `_reservation_cost`; add a test asserting the reservation scales with `n`; correct the documented bound.

#### M31 — OIDC `redirect_uri` falls back to the unauthenticated `Host` header when `OIDC_REDIRECT_URI` is unset

- **Where:** `infrastructure/web/session/sso.py:36-41` (`_redirect_uri` → `request.base_url`); no trusted-host allowlist anywhere in `app.py`/`config.py`; `.env.sample` documents the variable as optional ("otherwise it is derived from the incoming request").
- **Issue:** `request.base_url` derives from the raw `Host` header. With `OIDC_REDIRECT_URI` unset, a forged `Host` makes the gateway declare an attacker-influenced `redirect_uri` in the authorization request. End-to-end exploitability depends on the IdP's redirect-URI validation (exact-match registries are safe; wildcard/prefix policies are not) — but the gateway is the only place `redirect_uri` is decided and currently offers no defense of its own.
- **Fix:** Require `OIDC_REDIRECT_URI` whenever SSO is enabled outside local dev (fail fast at startup, mirroring the `JWT_SECRET`/`MASTER_KEY` production checks), or validate `Host` against a configured allowlist before deriving `base_url`.

#### M32 — `completion_service.py` has accreted five concerns; split the metering/billing collaborator out before routing lands

- **Where:** `application/completion_service.py` (576 lines — largest application module by ~180 lines; `_metered_stream` spans `:417-515`).
- **Issue:** One file now fuses token estimation (7 module helpers), the `InFlightSpend` reservation state, billing + outbox fallback, success/error tracing, dispatch orchestration + budget gate, and streaming metering. `_metered_stream` alone carries usage capture, estimation fallback, reservation release, shielded settlement, error-vs-ok trace branching, and billing — the single most complex function in the codebase, followable today mainly thanks to heavy comments. Note that **M26, M27, M29, and M30 all live in this file**: the defect density is itself the signal. The announced v2 routing features land squarely on this hot spot.
- **Fix:** Extract a `UsageMeter`/billing collaborator (`InFlightSpend` + `_record_usage`/`_observe`/`_bill` + the settlement body) leaving `CompletionService` as request orchestration. Do it while the code is calm, before routing.

### LOW (Round 4)

- **L19 — Stream estimation fallback counts only `choices[0]`** (`completion_service.py:73-82`, `_chunk_output_text`). For an `n>1` stream that disconnects/errors before the authoritative usage chunk, `streamed_chars` misses the other `n-1` choices → the estimate under-bills by ~`n`×. *Fix: sum deltas across all `choices[]` indices.*
- **L20 — Lockout escalation (`lockout_cycles`) is not updated atomically** (`application/user_service.py:228-252` reads the pre-attempt in-memory snapshot; `user_repository.py:83-89` `set_login_lock` is an unconditional UPDATE). Two concurrent threshold-crossing failures can each write `cycles + 1` from the same stale read, losing an escalation step — weakens (not breaks) the M25 exponential curve. *Fix: compute `lockout_cycles + 1` server-side in the UPDATE, like the failed-attempts counter already does.*
- **L21 — MLflow tracking server published to the host with no authentication** (`docker-compose.yml`: `mlflow` maps `5000:5000`, unlike `db`/`redis` which stay internal). Anyone reaching the port can read every team's per-call cost/token/model telemetry and delete/tamper traces via the read-write API — in a compose file that bills itself prod-like. *Fix: drop the host mapping (internal-only) or front with an auth proxy; mark the mapping dev-only.*
- **L22 — Image generation is entirely outside billing** (`from_imagen_response` in `vertex_adapter.py:146-158` and DALL·E responses carry no token usage; the cost model is per-token only). Every image call bills 0 and reserves ~0 — a missing per-image pricing field rather than a logic bug, but image spend is invisible to budgets. *Fix: add per-image pricing to `Model` when images matter; until then document the gap in `docs/usage-cost.md`.*
- **L23 — Reservation is computed from the pre-sanitize request** (`_prepare` calls `_enforce_budget` with the raw request; `sanitize_request` clamps `max_tokens` to 32k afterwards). A `max_tokens: 999999999` stream reserves an enormous phantom cost held until settlement — revenue-safe, but a team member can self-DoS their team's gate. *Fix: reserve from the sanitized request (clamp first, reserve second).*
- **L24 — API-key revocation wiring diverged; one wrapper is dead code** (`application/service.py:100-104`). `revoke_for_service_principal` is used by `ServicePrincipalService`, but the sibling `revoke_personal_keys_for_user` is never called — `UserService.set_user_active` (`user_service.py:341`) reaches directly to the repository port instead. Both wrappers also drop the `revoked_at: datetime` annotation behind `# noqa: ANN001`. *Fix: pick one collaboration style (through the service, or delete the dead wrapper), restore the annotation.*
- **L25 — `TeamController` mixes four sub-resources in 343 lines** (`infrastructure/web/teams/controller.py`): memberships, team API keys, budgets, and usage reporting — billing concerns bolted onto a membership controller. Each handler is individually fine; early-warning, and the natural split point (`BudgetController`/`UsageController`) is obvious. *Fix: split when it next grows.*
- **L26 — Doc/naming drift**: `README.md:189` roadmap item 2 still says "*Postgres service is stubbed pending the Postgres item*" while item 4 and the Deployment section document Postgres as shipped; `config.py:10` still defaults to `api_keys.db` — the filename from the project's API-key-manager origins (dev-only, harmless, but stale branding for an LLM gateway). *Fix: drop the stale note; consider renaming the default DB file.*

### Verified clean (Round 4 — checked specifically, no issue)

- **Non-streaming `_dispatch` is NOT exposed to the H13 disconnect-cancellation gap** — verified against the installed Litestar source: disconnect-driven scope cancellation is wired only into `ASGIStreamingResponse.send_body` (SSE/stream responses); a plain `Response` has no disconnect watcher, so a client drop during a non-streaming call never delivers `CancelledError` into the handler. `_dispatch`'s unshielded `_observe` is safe in this framework version.
- **Reservation lifecycle on all *iterated* paths** — add/release pairs verified on normal completion, pre-dispatch failure, post-resolve-pre-stream failure (`except BaseException` → remove → raise), and stream teardown (release is synchronous, before the shield, so a slow DB can't block it). M27 is the only gap.
- **No gateway-level double-billing** — SDK retries live inside a single `call()`; `_record_usage` writes ledger *or* outbox, never both; reconcile is idempotent check-insert-delete per row; `spend_since`'s two SUMs can momentarily *under*-count at the reconcile instant, never double-count.
- **Budget-gate concurrency** — no checkpoint between `in_flight.total()` and `in_flight.add()` (re-verified); concurrent requests cannot both slip under the limit on the same replica.
- **Clients cannot suppress stream usage** — `include_usage` forced on for OpenAI streams (`openai_adapter.py:115`); Anthropic/Gemini adapters synthesize a trailing usage chunk.
- **Error-translation layer doesn't swallow cancellation** — `translate_stream`/`arun_translated` catch `Exception`, never `BaseException`; `CancelledError`/`GeneratorExit` pass through.
- **Service principals, lockout mechanics (beyond L20), admin unlock** — team scoping, two-layer `enabled` kill switch, JWT-only SP administration, decoy-hash timing parity, exponential arithmetic (incl. the `min(cycles, 16)` overflow guard) all re-verified.
- **Auth surface** — HS256 pinned, dot-count JWT/key discrimination safe, revocation checked per request independent of the `last_used_at` throttle; no cookie-based CSRF surface (bearer-header auth; the only cookies are the short-lived `httponly`+`lax` SSO flow cookies).
- **Deployment surface** — non-root image, `FORWARDED_ALLOW_IPS` defaults to loopback (H9 fix in place), fail-fast on SQLite-in-production (M18 fix in place), CI has no `pull_request_target`/secret-vs-untrusted-checkout issues, `pip-audit` + `detect-secrets` + 80% coverage gate present. `.env.sample`/`justfile` clean.
- **Hexagonal boundaries** — `domain/` imports stdlib + `pwdlib`/`anyio` only; `application/` has zero `infrastructure`/`sqlalchemy`/`litestar` imports (grep-verified again, all new modules included). `domain/ports.py` still coherent; the `Transaction` port now documents the convention that resolves the old M11 ambiguity.
- **`test_stream_usage_fallback.py` is real coverage, not tautology** — it exercises benign `aclose()` vs genuine scope cancellation, asserts exact estimated token counts, distinguishes ok-vs-error traces, and covers authoritative-usage-wins and all-zero-usage edge cases.

---

## Round 3 — Money & concurrency review (2026-07-03)

Third whole-codebase pass, focused on the ~28 commits merged since Round 2: team budgets +
pre-call enforcement, login lockout, usage outbox + reconciler, MLflow ops metrics, upstream
error classification, distributed locks, unit-of-work refactor, and service principals with
scoped API keys. Run by four parallel reviewers (security, Python/async, architecture,
billing/concurrency); every finding below was **re-verified against source** before inclusion.
Baseline: **303 tests pass**, `ruff` and `pyrefly` clean, no tracked secrets.

The dedicated security pass found **no new security issues** — budget-gate authorization,
service-principal scope enforcement (two-layer `enabled` checks), lockout counter atomicity,
OIDC nonce+PKCE, secret-free logs/traces, and the new migrations' defaults all verified clean.
What Round 3 did surface is concentrated on the **billing/settlement path**: the new
outbox/reconciler machinery is well built, but the streaming settlement is not
cancellation-safe, and a few edges of the outbox design undercut the guarantees it was
built to provide.

New counts: **0 CRITICAL · 1 HIGH · 5 MEDIUM · 8 LOW**.

### Resolution status (Round 3, updated after remediation)

**Fixed & merged to `main`:**

| Finding | Fix PR |
|---|---|
| H13 — streaming settlement shielded from disconnect cancellation | #91 |
| M22 — outbox preserves the original event time | #92 |
| M23 — poison-message quarantine (attempts/last_error + migration) | #93 |
| M21 — budget gate counts spend sitting in the outbox | #94 |
| M24 — in-flight cost reservation bounds burst overshoot | #95 |
| M25 — exponential lockout + decay + admin unlock endpoint | #96 |
| L12 — Redis-outage lock no longer leaks the client | #97 |
| L17 — stale `_as_utc` comment corrected | #98 |
| L18 — `MLFLOW_METRICS_INTERVAL` validation coverage | #99 |
| L16 — README updated on shipped money controls | #100 |
| L13 — outbox guarantee level documented (at-most-once on crash) | #101 |
| L14 — unguarded reconciler documented as intentional | #102 |
| L11 — usage streamed before a provider failure is billed | #103 |

**M24** shipped as the deliberately simpler option (owner's call): an in-memory per-replica
reservation (prompt estimate + requested max-tokens ceiling) instead of a durable reservation
table; the honest residual bound is documented in `docs/usage-cost.md`. **M25** shipped as
exponential lock + decay with a platform-admin unlock (`DELETE /users/{id}/lock`) per the
owner's direction.

**Not changed (deliberate):**

- **L15** — budget compared as floats: per the finding, flag again only if invoice-grade
  accounting becomes a goal (integer micro-USD then).

### HIGH (Round 3)

#### H13 — Client disconnect mid-stream cancels the billing write: unbilled, untraced, unlogged

- **Where:** `application/completion_service.py:306-366` (`_metered_stream`), `:107-132` (`_record_usage`); trigger is Litestar's streaming response cancelling its anyio scope on `http.disconnect`.
- **Issue:** On a real client disconnect, cancellation is delivered at the provider await (`async for chunk in stream`, line 330) as `CancelledError` — a `BaseException` that the `except Exception` at line 338 does not catch, so `error` stays `None` and the `finally` proceeds to `await self._observe(...)` **inside the already-cancelled scope**. Its first checkpoint (the DB commit in `usage.record`) immediately re-raises `CancelledError`, which also sails through both `except Exception` guards in `_record_usage` (lines 115, 119). Net result: **no ledger row, no outbox row, no ERROR log, no trace**. The docstring ("a client disconnect — GeneratorExit — still records as 'ok'") and `tests/test_stream_usage_fallback.py` only cover the benign `aclose()`/`GeneratorExit` path from a non-cancelled context — the production disconnect path is the cancellation one, and it is unbilled.
- **Impact:** Any `stream: true` request whose client drops mid-stream consumes provider tokens with zero billing, zero budget consumption, and zero observability. Deliberately exploitable: read ~the whole completion, drop the connection before the final chunk → free inference. Partially reopens round-1 **C1** under client control.
- **Fix:** Shield the settlement — wrap the `finally` body (estimate + `_observe`) in `with anyio.CancelScope(shield=True):` (or run it on a shielded task with its own session). Add a regression test that iterates the stream inside a task group, cancels the scope at the provider await, and asserts a `UsageEvent` still lands.

### MEDIUM (Round 3)

#### M21 — Budget gate is blind to spend sitting in the outbox

- **Where:** `application/completion_service.py:243-259` (`_enforce_budget`) reads `usage_repository.spend_since` (`usage_repository.py:107-116`), which sums **only** `usage_event`.
- **Issue:** When ledger writes fail and events dead-letter to `pending_usage_event` (the exact failure mode the outbox was built for — a transient DB blip under load), that cost is real (already billed upstream) but invisible to every budget check until the reconciler drains it (60s cycle). During a sustained write degradation the gate under-counts for the whole degradation window.
- **Impact:** Silent budget-cap bypass precisely during the incident the outbox is designed to survive. Not covered by any test (`test_budget_enforcement.py` feeds `spend_since` directly; `test_usage_outbox.py` never asserts gate visibility).
- **Fix:** Have `spend_since` union `usage_event` with `pending_usage_event` (its `cost`/`event_created_at` columns carry what's needed), or explicitly document the gap as an accepted tradeoff in `docs/usage-cost.md` with a regression test proving the bound.

#### M22 — Reconciled usage events are re-stamped with insert time; `event_created_at` is stored but never used

- **Where:** `usage_repository.py:150-163` (reconcile insert passes no `created_at`); `orm.py:216` + `usage_repository.py:133` (outbox faithfully stores `event_created_at`, then ignores it).
- **Issue:** Events dead-lettered on July 31 and drained on Aug 1 land with `created_at` = reconcile time → the spend counts against **August's** budget window and the wrong monthly aggregate. The field that exists to prevent exactly this is dead code.
- **Fix:** Pass `created_at=row.event_created_at` in the reconcile insert (and `created_at=event.created_at` in `record` for consistency).

#### M23 — Outbox has no poison-message handling; oldest-first `LIMIT` lets poisoned rows starve newer events

- **Where:** `usage_repository.py:138-170` (`ORDER BY created_at LIMIT limit`, per-row `except → rollback`, no attempt counter); `usage_reconciler.py` (batch 200, 60s).
- **Issue:** `usage_event` has FKs on `team_id`/`api_key_id`; `pending_usage_event` has none. A team/key deleted while its events sit in the outbox makes those rows fail the ledger insert on **every** cycle, forever. Once ≥200 permanently-failing rows accumulate, the oldest-first select returns only poison — newer pending events are never selected again (silently dropped from reconciliation) while the log emits one warning per row per minute.
- **Fix:** Track `attempts`/`last_error` on the pending row and quarantine after N failures; at minimum rotate the batch window so poison can't monopolize the head.

#### M24 — Budget-gate burst overshoot is unbounded; the "bounded overshoot" docstring oversells it

- **Where:** `application/completion_service.py:243-259` (docstring + gate); streams settle only at stream end (`:341-366`).
- **Issue:** The pre-dispatch check reads committed spend with no reservation, no per-team in-flight cap, and no request-cost floor: N concurrent requests under a nearly-exhausted budget all pass. Streams widen the window from milliseconds to minutes — hundreds of long `max_tokens` streams can be admitted within one blind spot, none of which debits the window until it finishes. Overspend = in-flight × per-request cost, i.e. bounded by nothing in the code; the round-2 acceptance ("bounded overshoot, same semantics as other gateways") is only true per request, not per burst. Combined with H13, a disconnecting streamer never even settles the debit.
- **Fix (if a harder cap is wanted):** debit a pessimistic reservation (`max_tokens` × output price) at admission and reconcile at settlement, or add a per-team in-flight cap. Otherwise state the honest bound (N × cost) in the docstring and `docs/usage-cost.md`.

#### M25 — Account lockout is sustainable indefinitely by an attacker; the in-code comment claims the opposite

- **Where:** `application/user_service.py:52-57` (comment: "temporary, so an attacker can't permanently deny the victim access"), `MAX_FAILED_LOGINS = 5`, `LOCKOUT_DURATION = 15min`.
- **Issue:** The mechanics are otherwise solid (atomic in-DB counter, no re-lock during the lock, decoy-hash timing parity), but after each 15-minute expiry, 5 wrong passwords re-lock the account: **20 requests/hour from a single IP** (well under the 20/min auth rate limit) keeps a victim's password login locked forever, correct password rejected throughout. SSO is unaffected and an admin reset exists — mitigations, not prevention.
- **Fix:** Exponential lockout with cap + counter decay, or step-up/CAPTCHA instead of a hard lock after repeat cycles; at minimum correct the comment and alert on repeated lock cycles for one account.

### LOW (Round 3)

- **L11 — Mid-stream provider error discards all usage streamed before the failure** (`completion_service.py:338-344`). On a provider error the gateway emits only an error trace; tokens already streamed (paid upstream) are never billed or budget-counted. Deliberate per the docstring, but a provider dying at 95% of a long stream systematically under-bills. Not client-controllable. *Fix: on the error path, bill the estimate from `streamed_chars` — the machinery already exists two lines below.*
- **L12 — `RedisDistributedLock.hold` leaks the client if `acquire()` raises; a Redis outage silently skips rotation fleet-wide** (`infrastructure/locks.py:41-54`). Both `Redis.from_url` and `lock.acquire()` run before the `try`, so an exception there never reaches `client.aclose()`; the exception bubbles to the rotation loop, which logs and skips that day's run on **every** replica. *Fix: move `acquire()` inside the `try` (acquire-failure ⇒ `acquired=False`), and surface skipped rotations as a metric/alert.*
- **L13 — Crash between upstream success and the usage write loses the billing record** (`completion_service.py:221-241`). The outbox is a dead-letter for failed writes, not a write-ahead intent: a process kill between the provider response and `record` (ms for non-streaming; the whole settlement for streams) leaves no durable artifact. Inherent to the design — *fix: document the guarantee level ("at-most-once on crash") in the outbox docstring; a true fix is a pre-dispatch intent row.*
- **L14 — Usage reconciler runs unguarded on every replica** (`usage_reconciler.py:41-47` vs `rotation.py`'s `guarded_rotate`). Verified safe (idempotent check-insert-delete per row; the loser of a PK race rolls back), so this is wasted redundant work + DB contention scaling with replica count, and an inconsistency with the `DistributedLock` convention introduced in the same commit range. *Fix: reuse the lock port, or document that idempotency makes the lock intentionally optional.*
- **L15 — Budget limit and spend compared as floats** (`domain/entities.py:58` `limit_cost: float`; `spend_since` `SUM` of a float column vs `>=` at `completion_service.py:255`). Extends the existing `cost: float` pattern, but budgets are the first place the value *gates* a request. Drift at realistic volumes is far below enforcement granularity — flag only if invoice-grade accounting becomes a goal. *Fix (then): integer micro-USD, as `MetricsAggregator._cost_micro_usd` already does.*
- **L16 — README feature list is stale on shipped money controls** (`README.md` item 10: "*Pre-call budget enforcement is still a follow-up; streaming calls aren't token-counted yet.*"). Both claims are now false (`_enforce_budget` gates every path including streams — `test_budget_enforcement.py::test_streaming_is_gated_too`; `_metered_stream` meters usage). Service principals, scoped keys, and login lockout are also missing from the shipped list. `docs/usage-cost.md` is up to date; the front-door README is not. *Fix: update item 10 and add the new capabilities.*
- **L17 — Stale justification comment on `_as_utc`** (`application/service.py:31-33`). Says "SQLite reads timestamps back naive", but every mapped datetime column goes through Advanced Alchemy's `DateTimeUTC`, which re-attaches UTC on read (SQLite included) — the guard is defensive-only, not a live gap. *Fix: correct or drop the comment so future maintainers don't "fix" other already-safe comparisons.*
- **L18 — `MLFLOW_METRICS_INTERVAL` lacks the config-validation test its siblings have** (`config.py:213`). Logic verified fine (`minimum=0`, `0` disables the publisher via `app.py`), but it's the only numeric env var with zero coverage in `test_config.py`. *Fix: add the two cases (0 disables; negative raises).*

### Verified clean (Round 3 — checked specifically, no issue)

- **Budget authorization** — `set_budget`/`delete_budget` are platform-admin only; a team admin cannot raise their own cap. All six inference paths (chat/responses/embeddings/images + both streams) route through `_prepare` → `_enforce_budget`; no path skips the gate.
- **Service principals / scoped keys** — management-only keys rejected on `/v1/*`; `sp.enabled` re-checked independently at both the auth layer and team-management layer (disable = kill switch, regression-tested); management-scope keys mintable only for SPs; SP administration JWT-only, so a leaked key cannot self-replicate. JWT-vs-key discrimination via dot-count is safe (`token_urlsafe` alphabet has no `.`).
- **Login lockout mechanics** — atomic `UPDATE … RETURNING` counter (no cross-worker lost updates); locked accounts don't extend their own lock; decoy-hash timing parity on all no-op branches; `is_active` enforced per request.
- **Outbox idempotency & no double-billing** — one write path per request (ledger *or* outbox, never both); reconcile is check-insert-delete-commit in one transaction per row; a lost commit-ack converges without re-insert; two replicas racing the same row is safe (PK conflict → rollback → next cycle).
- **UTC consistency** — `window_start` and all timestamps aware-UTC end to end (Advanced Alchemy `DateTimeUTC` verified on SQLite and Postgres); composite `(team_id, created_at)` index backs the gate query.
- **Upstream error mapping** — uniformly applied on every dispatch path incl. mid-stream (`translate_stream`); timeout-before-connection ordering correct; no upstream bodies echoed. `InvalidBudget`/`InvalidKeyScope` fall through to the 400 default correctly; `BudgetExceeded` → 402 with the mapped handler.
- **Rotation + distributed lock** — `guarded_rotate` acquire/skip/release-on-exception correct; TTL (15 min) ≪ daily interval, so a crashed holder self-heals; no realistic two-holder overlap.
- **Observability internals** — `MetricsAggregator` counters guarded by a real `threading.Lock` across worker threads; `abandon_on_cancel=True` on blocking MLflow calls so shutdown never hangs; all four background loops (reconciler, rotation, dispatcher, metrics) start/stop symmetrically; no task leaks.
- **Hexagonal boundaries** — zero `infrastructure`/`sqlalchemy`/`litestar` imports in `domain/` and `application/` (grep-verified, including all new modules). No new string-built SQL anywhere.
- **New migrations** — lockout and scope columns default existing rows to the safe state; budget table's unique `team_id` + upsert-retry handles the concurrent-insert race.

---

## Round 2 — Enterprise-readiness review (2026-07-02)

Second whole-codebase pass focused on the "enterprise-ready LLM gateway" goal, run by four
parallel reviewers (security, Python/async, architecture, general quality) and verified against
source. Baseline is healthy: **188 tests pass**, `ruff` and `pyrefly` clean, no committed
secrets (`.env`/`*.db` are gitignored, placeholders only), no `TODO`/`print` residue.
IDs continue the round-1 sequence to avoid collision.

New counts: **0 CRITICAL · 7 HIGH · 7 MEDIUM · 4 LOW** + an enterprise-gap roadmap.

### Resolution status (Round 2, updated after remediation)

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

---

### CRITICAL

#### C1 — Streaming inference is completely unbilled and untraced

- **Where:** `application/completion_service.py:143-159` (`open_chat_stream`, `open_responses_stream`); `infrastructure/web/api_router/completions.py:58-61, 82-84`
- **Issue:** The non-streaming paths call `_observe(...)` to persist a `UsageEvent` (billing) and emit a trace. The streaming paths return the provider iterator directly and **never call `_observe`**, and the controller's SSE wrappers only serialize chunks. No usage row, no cost, no trace is recorded for any `stream: true` request.
- **Impact:** A customer who sets `stream: true` (the default for many OpenAI clients) consumes upstream provider tokens with **zero billing and zero observability**. Usage aggregates under-report; cost attribution is bypassed entirely.
- **Fix:** Accumulate token usage while iterating the stream (request `stream_options: {include_usage: true}` for OpenAI-compatible providers so the final chunk carries `usage`) and call `_observe` when the stream completes; wrap the generator so it records even on early client disconnect.

---

> **Note:** the earlier H1 ("credentials not scoped to an org/team") was resolved as
> **by design** after a product decision — credentials are an intentionally global pool
> managed centrally by cloud ops, and all orgs pull from the same set. See the
> "NOT issues / by design" section below. HIGH items renumbered accordingly.

### HIGH

#### H1 — No "last admin" protection: a team admin can orphan a team

- **Where:** `application/team_service.py:147-156` (`set_role`), `158-163` (`remove_member`)
- **Issue:** Neither method guards against demoting/removing the **last** admin (or the platform admin's own membership on the team).
- **Impact:** A team admin can `DELETE`/demote every other admin, leaving the team with no team-level administrator (recoverable only by a platform admin), or strip the platform admin's membership to reduce oversight.
- **Fix:** Before demote/remove of an ADMIN membership, assert at least one other ADMIN remains; forbid a team admin from removing the platform admin's membership.

#### H2 — Invite tokens never expire

- **Where:** `domain/entities.py` `Invite.is_usable` (checks only `used_at`); `application/user_service.py:81-90` (`create_invite`)
- **Issue:** Invites are single-use but have **no `expires_at`**. A leaked invite (logs, chat, screenshot) is a valid registration path indefinitely.
- **Impact:** A captured invite token can be redeemed months later to create an account.
- **Fix:** Add `expires_at` (mirror `PasswordReset`, e.g. 24-72h TTL) and check it in `is_usable`; add a migration + column.

#### H3 — Master key derived from a single unsalted SHA-256 pass

- **Where:** `infrastructure/crypto.py:20-22` (`_derive_fernet_key`)
- **Issue:** The envelope-encryption master key (from `SALT_KEY` / `JWT_SECRET`) is `base64(sha256(secret))` — no salt, no slow KDF. This is fine for a **high-entropy random** master, but offers no protection if an operator uses a human-chosen passphrase.
- **Impact:** If `SALT_KEY` is a low-entropy passphrase and the DB keyring is exfiltrated, an attacker brute-forces it offline at GPU speed, unwraps every data key, and decrypts all stored provider credentials.
- **Fix:** Either enforce/validate high-entropy keys at startup (length + randomness), or derive via a memory-hard KDF (scrypt/argon2) with a stored per-deployment salt. At minimum, document the requirement that these be random 32-byte values.

#### H4 — Default JWT secret is only rejected for exactly `production`/`prod`

- **Where:** `config.py:34` (`_PRODUCTION_ENVIRONMENTS`), `88-93` (`__post_init__`)
- **Issue:** The publicly-known `DEFAULT_JWT_SECRET` is only rejected when `ENVIRONMENT` is exactly `production`/`prod`. Any other internet-facing value (`staging`, `prod` with a space, a typo like `prodction`) silently signs JWTs with the git-committed default.
- **Impact:** An attacker who knows the default secret forges an admin JWT and takes over the gateway.
- **Fix:** Invert the default — require an explicitly-set strong `JWT_SECRET` **unless** environment is a known-local value (`development`/`test`), or fail fast on the default secret in every non-local environment. Trim/normalize `ENVIRONMENT`.

#### H5 — Provider SDK clients are created per request and never closed

- **Where:** `infrastructure/llm/openai_adapter.py:64-130, 143-148`; `infrastructure/llm/anthropic_adapter.py` (async methods)
- **Issue:** Each call builds a fresh `AsyncOpenAI` / `AsyncAnthropic` (each owns an httpx connection pool) and never `aclose()`s it — the same leak pattern already fixed for the SSO `AsyncOAuth2Client`.
- **Impact:** Under sustained traffic, connection pools/sockets accumulate until GC, risking `Too many open files` on the worker; streamed responses that the client abandons also leave the SDK stream open.
- **Fix:** Reuse a long-lived client per (provider, credential) or wrap per-call clients in `async with`/`try…finally: await client.aclose()`; close streams on generator finalization.

---

### MEDIUM

#### M1 — JWT signing keys can be deleted while dependent tokens are still valid

- **Where:** `infrastructure/keyring.py:84-91` (`rotate_jwt`)
- **Issue:** Keys are deleted when `created_at < now - max_age`, but a key is *active for signing* for up to one rotation interval after creation. A token signed late in a key's active window outlives the key's deletion by up to ~1 interval → premature "invalid token" mid-session.
- **Fix:** Use `created_at < now - (max_age + rotation_interval)` (or retire-then-delete based on when the key stopped being active).

#### M2 — JWT master cipher accepts an empty secret (fixed, public key)

- **Where:** `infrastructure/keyring.py:39-42`; `infrastructure/crypto.py:33-34`
- **Issue:** `_master(JWT)` calls `MasterCipher(self._jwt_master_key)` with no empty check, unlike the credential master (`build_master_cipher` raises). `MasterCipher("")` derives a fixed key from `sha256("")`. Reachable in non-production if `JWT_SECRET=""` is set explicitly.
- **Fix:** Route the JWT master through a presence check too; combined with H5, validate `JWT_SECRET` in all non-local environments.

#### M3 — `SALT_KEY` has no startup fail-fast

- **Where:** `config.py:88-93` (validates only `JWT_SECRET`)
- **Issue:** Unlike `JWT_SECRET`, missing `SALT_KEY` isn't caught at startup. The app boots healthy and credential operations later fail with `SaltKeyMissing` (503) only at first use. (Credentials are *not* written unencrypted — the operation fails closed — so this is a late-failure/ops issue, not a data-exposure one.)
- **Fix:** If credential features are expected in production, assert `SALT_KEY` presence in `__post_init__`.

#### M4 — Key rotation is not atomic across steps

- **Where:** `infrastructure/rotation.py:49-55` (`rotate_all`); `infrastructure/keyring.py` create/retire; `credential_repository.reencrypt_all`
- **Issue:** `new_credential_key → reencrypt_all → retire_old → rotate_jwt` run as separate commits in one session. A crash mid-sequence leaves a partially-rotated keyring. This is **non-destructive** (retired keys stay readable and each ciphertext records its key id, so un-migrated rows still decrypt, and the next run re-encrypts), but the "exactly one active credential key" invariant can transiently break.
- **Fix:** Wrap the sequence in a single transaction, or make it idempotent/resumable and log partial-rotation state.

#### M5 — Unbounded queries (no pagination)

- **Where:** `credential_repository.py:57` (`list`), `72-85` (`reencrypt_all`); `model_repository`/`api_key` `list_by_team`; `usage_repository.aggregate`
- **Issue:** These `SELECT`s have no `LIMIT`. `reencrypt_all` also materializes the whole credential table into memory in one transaction.
- **Impact:** A team with many models/keys or a large usage history makes a single list/dashboard/rotation call load unbounded rows → latency/OOM and long lock holds.
- **Fix:** Add pagination to list endpoints; batch `reencrypt_all` (paged commits).

#### M6 — Usage-recording failure is silently swallowed

- **Where:** `application/completion_service.py:69-85`
- **Issue:** `_observe` wraps `usage.record` in `except Exception: logger.warning(...)` (fail-safe by design so billing never breaks a served request). But a DB hiccup means the request is served (and billed upstream) with **no usage row**, permanently under-counting.
- **Fix:** Keep fail-safe behavior but add a durable fallback (retry queue / dead-letter / metric+alert) so dropped billing events are recoverable and visible.

#### M7 — `Model.update` cannot reset an optional field back to `None`

- **Where:** `application/model_service.py:88-93`
- **Issue:** `update` applies `{k: v for … if v is not None}`, so a previously-set optional field (`api_version`, `input_cost_per_token`, `output_cost_per_token`) can never be cleared to `null`. (`enabled=False` works — `False` is not `None`.)
- **Fix:** Distinguish "field omitted" from "field set to null" (e.g. a sentinel/`UNSET` marker or an explicit updatable-fields set).

#### M8 — Vertex credential parsing can surface key material on error

- **Where:** `infrastructure/llm/vertex_adapter.py` (client build: `json.loads` + `service_account.from_service_account_info`)
- **Issue:** Malformed stored credential JSON raises `JSONDecodeError`/`ValueError` inside the request path with no translation; depending on error handling this can log/echo fragments of the service-account JSON (which contains the private key).
- **Fix:** Wrap credential parsing and raise a `CredentialMisconfigured` domain error with a non-revealing message; never include the raw value in the exception.

#### M9 — Unauthenticated OpenAPI docs expose the full admin surface

- **Where:** `app.py:58-60, 113-127` (`OpenAPIConfig(path="/", render_plugins=[…])`)
- **Issue:** Swagger (`/`), Scalar, Stoplight, and `/openapi.json` are mounted publicly with no guard, disclosing the complete schema (credential, org/team-admin, invite endpoints).
- **Impact:** Schema disclosure aids targeted attacks. (Common to expose intentionally, hence MEDIUM.)
- **Fix:** Gate docs behind auth in production, or make exposure an explicit config toggle.

#### M10 — Numeric env vars parsed with no validation

- **Where:** `config.py:105-108` (`int()`/`float()` on `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `REQUEST_TIMEOUT`, `MAX_RETRIES`)
- **Issue:** Bare `int()/float()` — a unit suffix (`60s`) or negative value crashes startup with an opaque `ValueError` (no field context) or is silently accepted (negative pool size).
- **Fix:** Parse with field-named errors and range validation (`> 0`).

#### M11 — Inconsistent transaction boundaries in the persistence layer

- **Where:** `membership_repository.py` (only flushes, relies on service unit-of-work) vs every other repo (commits internally)
- **Issue:** Mixed conventions are a maintenance trap: a new caller of `membership_repository.add/remove` without a `_unit_of_work()` wrapper silently loses the write.
- **Fix:** Standardize — either all repos commit internally, or all defer to an explicit unit of work.

#### M12 — Streaming SDK response not closed on client disconnect

- **Where:** `infrastructure/llm/openai_adapter.py:84-104`; consumers in `completions.py`
- **Issue:** The async generator yielding chunks doesn't close the underlying SDK/httpx stream if the client disconnects mid-stream.
- **Fix:** Use `try/finally` around the `async for` to `aclose()` the stream on generator finalization.

#### M13 — Provider adapters and the gateway have no unit tests

- **Where:** `infrastructure/llm/gateway.py`, `openai_adapter.py`, `anthropic_adapter.py`, `vertex_adapter.py`, `responses_emulation.py`
- **Issue:** No `test_gateway`/adapter tests; only `request_policy` and `completions` are covered. Capability routing and request/response translation are untested.
- **Fix:** Add adapter tests with mocked SDK clients (translation shapes, capability dispatch, error mapping).

---

### LOW

- **L1 — Rate limit is per-IP via untrusted remote addr** (`infrastructure/web/rate_limit.py`). Behind a proxy without forwarded-header trust, all clients collapse to one IP (global throttle / attacker not distinguished). Documented in the module, but no startup assertion. *Fix: require proxy-header config or a trusted-proxy setting.*
- **L2 — Missing `tv` claim defaults to 0** (`infrastructure/web/session/jwt.py:40`). A validly-signed token lacking `tv` is treated as version 0 and accepted for a never-logged-out user. Narrow, needs a valid signature. *Fix: require `tv` or reject tokens without it.*
- **L3 — No admin-forced session revocation / account-disable** (`infrastructure/web/session/dependencies.py:37`). An admin cannot invalidate a compromised user's 7-day JWT; only the user's own `/logout` bumps `token_version`. *Fix: add an `is_active`/disabled flag checked at auth, and an admin revoke that bumps `token_version`.*
- **L4 — Request DTOs lack format/length validation** (`web/*/schemas.py`). `email`/`name`/etc. accept raw `str`; bad values surface as generic downstream errors instead of a 422, and unbounded strings reach the DB. *Fix: validated types / `max_length` on DTO fields.*
- **L5 — `DELETE` endpoints don't set explicit `204`** (`web/teams/controller.py` etc., vs `password_reset.py` which does). Relies on Litestar defaults; inconsistent status contract. *Fix: set `status_code=HTTP_204_NO_CONTENT` on delete handlers.*
- **L6 — `usage_event.model_name`/`operation` unindexed** (`persistence/orm.py:133-134`). `aggregate` filters/orders on `model_name` with no supporting index → seq scan as usage grows. *Fix: add a composite index for the aggregate query.*

---

### Reviewed and verified as NOT issues (or by design)

- **SQL injection** — none; all queries use SQLAlchemy Core/ORM with bound parameters, no `text()`/string interpolation.
- **JWT algorithm confusion** — not present; `Token.decode(..., algorithm="HS256")` pins the algorithm. Secret comparison is constant-time (`secrets.compare_digest`).
- **Tenant scoping for models & API keys** — enforced in the service layer (`ModelService._get_scoped`, `APIKeyService.revoke_for_team` check `team_id`). Repos are intentionally thin; this is a valid hexagonal split.
- **Credentials are a global, unscoped pool** (`domain/entities.py` `Credential` has no `team_id`/`organization_id`; `ModelService._validate_credential` checks only existence + provider) — **by design** (product decision). Provider credentials are managed centrally by cloud ops (platform-admin only, via `CredentialController`), and all organizations intentionally draw from the same pool; there is no per-org credential isolation to enforce. Since the encrypted secret is never returned and `api_base` is credential-fixed, a team can *use* a credential it references but never read it. Cost attribution is still per-team (recorded on `UsageEvent`).
  - *Optional future enhancement (nice-to-have, not required):* allow restricting a credential to one org via a **nullable** `organization_id` on `Credential` — `NULL` = global (default, current behavior), set = usable only by teams of that org. `_validate_credential` would then accept when `credential.organization_id is None or == team.organization_id`. Backward-compatible (existing credentials stay global) and opt-in.
- **SSRF via `api_base`** — mitigated by design: the endpoint comes only from the admin-managed credential, never the team-controlled model (`openai_adapter._base_url` comment).
- **SSO adopting an existing account by email** — intentional after the recent SSO hardening: adoption requires `email_verified` **and** an account not already bound to a different `sub`; role is re-synced from IdP groups (IdP as source of truth). This is the designed JIT-linking behavior, not a bug.
- **Response DTOs leaking secrets** — none; `UserResponse` exposes only `id/email/is_admin/created_at` (read-only), no `password_hash`/`token_version`/`key_hash`; credential `values` are never returned.
- **Sync gateway methods "blocking the event loop"** — the async web path uses only the `a*` methods; the sync methods are documented as library-only. Not a live defect (would only bite a future misuse).
