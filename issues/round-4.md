# Code Review — Round 4 (2026-07-06)

[← Index](INDEX.md)

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

## Resolution status

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
- **Issue:** One file now fuses token estimation (7 module helpers), the `InFlightSpend` reservation state, billing + outbox fallback, success/error tracing, dispatch orchestration + budget gate, and streaming metering. `_metered_stream` alone carries usage capture, estimation fallback, reservation release, shielded settlement, error-vs-ok trace branching, and billing — the single most complex function in the codebase, followable today mainly thanks to heavy comments. Note that **M26, M27, M29, and M30 all live in this file** — the defect density is itself the signal. The announced v2 routing features land squarely on this hot spot.
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
