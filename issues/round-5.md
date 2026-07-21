# Code Review — Round 5 (2026-07-06)

[← Index](INDEX.md)

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

## Resolution status

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
