# Code Review — Round 8 (2026-07-09)

[← Index](INDEX.md)

Eighth pass, run right after the native-provider-endpoint work landed (PRs #209–#219:
native Anthropic `/v1/messages` and native Gemini `generateContent` /
`streamGenerateContent`, framework-agnostic conformance, OpenAI error envelope, and
the `x-api-key`/`x-goog-api-key` auth fallback). This round is a **fresh-eyes sweep
focused on that net-new surface** (commits `95a605e`..`fb7930c`) across four lenses —
security, correctness/async/money, architecture/tests, and Litestar/deps/perf — each
finding **read against source line-by-line**, and the top money/security findings
**empirically reproduced or cross-confirmed by more than one reviewer**. Everything in
Rounds 1–7 remains as documented there and is **not** re-reported here.

## Executive summary

The codebase remains healthy where seven prior rounds hardened it: the security core
(auth, tenancy, RBAC, SSO, SCIM, credential encryption, rate limiting), the money core
(`UsageMeter` admit→settle→release), the streaming disconnect/cancellation machinery,
and the hexagonal boundary are all intact and, for the native additions, correctly
reused in the hard parts (exactly-once settlement, shielded billing, `_prime`/`_rechain`
stream priming, per-call client lifecycle — all verified clean, with genuinely good
tests).

**The risk is concentrated in one theme: the new "verbatim passthrough" surface trusts
the client body too much and does not mirror the governance the OpenAI-compatible
surface enforces.** Because the native paths deliberately skip `sanitize_request` /
`clamp_output_tokens` and forward the raw client JSON, they reopen a class of problems
the OpenAI surface already closed:

- **A CRITICAL credential-override / open-relay vector** on native Anthropic: the raw
  body is spread as `**kwargs` into the Anthropic SDK, whose reserved control kwargs
  (`extra_headers`, `extra_query`, `extra_body`, `timeout`) let a tenant replace the
  gateway's vaulted upstream credential and inject arbitrary outbound headers.
- **Money/budget governance regressions**: native Gemini admission reserves `$0`
  (in-flight burst guard defeated), the admin per-model output ceiling (`max_output_tokens`)
  is unenforced on both native surfaces (real unbounded upstream spend), and the Gemini
  non-streaming path silently drops the H14 estimate fallback (silent $0 billing when
  usage is absent).

None require a redesign — each is a localized fix bringing the native surface in line
with the OpenAI surface's existing guards. But the CRITICAL and the three HIGHs should
land **before this surface carries production traffic**. Overall quality of the new
feature is otherwise good (clean structure, strong streaming tests, faithful metering
in the settled-billing path); the gap is that "passthrough" was taken too literally on
the input/governance side.

Counts: **1 CRITICAL · 3 HIGH · 3 MEDIUM · 3 LOW.**

## Issue summary

| ID | Title | Severity | Files | Status |
|----|-------|----------|-------|--------|
| ISSUE-001 | Native Anthropic passthrough: client can override the gateway credential via `extra_headers` (kwargs injection / open relay) | critical | `infrastructure/llm/anthropic_adapter.py:205-245` | **Fixed** (#221) |
| ISSUE-002 | Budget reservation always `$0` on the native Gemini surface (in-flight burst guard defeated) | high | `application/usage_meter.py:38-62,120-127,149-160`; `application/completion_service.py:319,369` | **Fixed** (#221) |
| ISSUE-003 | Native endpoints skip `sanitize_request`/`clamp_output_tokens`: `max_output_tokens` ceiling (H15) unenforced + self-DoS from unbounded reservation (L23) | high | `application/completion_service.py:186-207,224-268,313-376`; `domain/request_policy.py:86,121-142` | **Fixed** (#221) |
| ISSUE-004 | Gemini non-streaming `generate_content` forks `_dispatch` and loses the H14 estimate fallback → silent $0 billing | high | `application/completion_service.py:297-348` (vs `:125-156`) | **Fixed** (#221) |
| ISSUE-005 | The Gemini passthrough uses the private `google-genai` `_api_client.async_request` with an open pin | medium | `infrastructure/llm/vertex_adapter.py:299,315`; `pyproject.toml:21` | **Fixed** (#222) |
| ISSUE-006 | Native methods resolve via the `"chat.completions"` capability slot: the gateway matrix doesn't protect them | medium | `infrastructure/llm/gateway.py:117-160`; `application/completion_service.py:225-229,314-318` | **Fixed** (#224) |
| ISSUE-007 | Native endpoints emit the OpenAI error envelope (not the provider's); the contract is neither tested nor documented | medium | `infrastructure/web/api_router/router.py:49`; `infrastructure/web/exception_handlers.py:160-174` | **Fixed** (#223) |
| ISSUE-008 | The prompt estimate (`_request_text`) ignores Anthropic's top-level `system` field → reservation under-count | low | `application/usage_meter.py:38-62` | **Fixed** (Round 8 close) |
| ISSUE-009 | Triplicated stream-metering skeleton (`_metered`/`_metered_native`/`_metered_gemini`, `metered_*_stream`) — DRY debt | low | `application/completion_service.py:270-295,378-404,515-543`; `application/usage_meter.py:392-569` | **Deferred** |
| ISSUE-010 | Test coverage asymmetry: no budget/concurrency test on the native Gemini surface | low | `tests/native/test_generate_content.py` | **Covered** (#221) |

## Findings

### ISSUE-001 — Native Anthropic passthrough: client can override the gateway credential via `extra_headers` (critical)

**Problem.** `anative_messages` builds `body = {**native_body, "model":
model.provider_model_id}` and calls `await client.messages.create(**body)`,
where `native_body` is the client's **raw JSON body** on `POST /v1/messages`,
forwarded "verbatim". But `messages.create()` is an **`anthropic` Python SDK**
method, not an HTTP POST: it accepts the reserved kwargs `extra_headers`,
`extra_query`, `extra_body`, `timeout` alongside the Messages API fields.
Verified against the installed SDK
(`anthropic/resources/messages/messages.py:143-146`, docstring: "Send extra
headers / Add additional query parameters / Add additional JSON properties /
Override the client-level default timeout") and against
`_base_client._build_headers`/`merge_headers`: custom headers **win** over the
defaults on collision, and `x-api-key` isn't in the `_APPEND_HEADERS` allowlist.
Reproduction: a body with `"extra_headers": {"X-Api-Key": "<anything>"}` makes
`_build_headers` return `x-api-key = <anything>`, replacing the vaulted
credential passed to `AsyncAnthropic(api_key=...)`.

**Why it's a problem.** It violates the project's explicit invariant ("the
client must never control the upstream credentials/base_url; server-side only")
for the auth headers, which are as sensitive as `base_url`. It's the same
kwargs-injection class that `domain/request_policy.py` / `sanitize_request`
block **for the OpenAI surface** (which builds kwargs field-by-field from an
allowlist, with no `**request`), reopened by the native path alone. The Gemini
path is **not** affected (it passes the body as a single positional dict to
`_api_client.async_request`, and the SDK strips `_`-prefixed keys).

**Impact.** Any authenticated user can turn the gateway into an **anonymous
relay** to the real Anthropic API using a credential of their choice (their own,
or a stolen one), routing traffic through the gateway's IP/infrastructure and
bypassing the team's configured credential entirely. Via
`extra_query`/`extra_body`/`timeout` they can also add arbitrary query/JSON
fields to the upstream request or force degenerate timeouts (resilience
bypass). Security + request integrity + possible credential abuse.

**Suggested fix.** Before spreading the body, **reject (400) or strip** the
reserved control keys (`extra_headers`, `extra_query`, `extra_body`, `timeout`,
and any `_`-prefixed key) — ideally in `prepare_native`/`native_messages` so it
covers both streaming and non-streaming — or pass the body as a single
validated dict instead of `**kwargs`, aligning with the (safer) Gemini form. Add
a regression test asserting that `extra_headers`/`extra_query`/`extra_body`/`timeout`
in the native body do **not** reach the outbound HTTP request.

### ISSUE-002 — Budget reservation always `$0` on the native Gemini surface (high)

**Problem.** The native Gemini body carries the prompt under `contents` and the
output ceiling under `generationConfig.maxOutputTokens` (nested). But
`_request_text` reads only `messages`/`input`/`instructions` and
`_max_output_tokens` only the top-level `max_tokens`/`max_completion_tokens`/
`max_output_tokens`. Neither key exists in a Gemini body, so `_reservation_cost()`
returns **`0.0`** for every native Gemini call, regardless of prompt/output size.
Empirically confirmed by two reviewers (`GEMINI native → reservation 0.0` vs
`ANTHROPIC native → honest reservation`). Native Anthropic isn't affected
because its Messages API uses exactly top-level `messages` and `max_tokens`.

**Why it's a problem.** `InFlightSpend` (the pre-call reservation) exists to
bound overshoot from a concurrent burst: each admitted request reserves its
pessimistic cost until settled, so a burst can't all pass under a nearly-
exhausted cap before the first settles (streams widen the window to minutes).
With a `$0` reservation, that protection is a **complete no-op** on the Gemini
surface — the same budget-cap bypass class R7-H22/M50 addressed for other
surfaces.

**Impact.** A team whose committed spend is near (but under) the cap can launch
N concurrent native Gemini requests (bounded only by the reachable 120/min
per-IP rate limit), all admitted because each contributes `0` to the reservation
the others see → overshoot past the hard cap by ~N × per-request cost. Final
billing stays correct (settlement reads the authoritative `usageMetadata`), so
it's not unbilled spend, but it's a real budget-overshoot window — worse for
long streams.

**Suggested fix.** Give `_reservation_cost`/`admit` a Gemini-aware path that
reads `contents[].parts[].text` and `generationConfig.maxOutputTokens` (+
`candidateCount`) when the body isn't OpenAI-shaped — mirroring how
`_gemini_usage` maps the native form in settlement. Add a test asserting a
non-zero reservation and `BudgetExceeded` throttling on concurrent native Gemini
bursts.

### ISSUE-003 — Native endpoints skip sanitize/clamp: max_output_tokens unenforced + self-DoS (high)

**Problem.** The OpenAI surface runs every request through `sanitize_request`
(clamp `max_tokens`/`n`) and `clamp_output_tokens` (apply the per-model
`model.max_output_tokens` ceiling, the Round 5 H15 fix) before `admit`. The
native path skips both by design (passthrough). Two consequences:

1. **The admin cost ceiling (`max_output_tokens`) isn't applied** to the native
   body: a client can request (and the provider **really bills**) output well
   past the admin's governance policy.
2. For Anthropic (where the reservation reads `max_tokens`) a client can set a
   huge `max_tokens` and inflate `InFlightSpend` without limit, failing the
   admission of all their other requests with `BudgetExceeded` until it settles
   — the reopening of L23 (Round 4), closed on the OpenAI surface by reserving
   from the sanitized body.

**Why it's a problem.** `docs/native-anthropic.md:8` and `docs/native-gemini.md:8`
explicitly promise the gateway "keeps the same governance as
`/v1/chat/completions` (auth, per-team budget, metering, rate limiting)" —
**false** for the cost-ceiling half. No test in `tests/native/`/`tests/conformance/`
references `max_output_tokens` (grep: zero hits).

**Impact.** Real upstream spend unbounded past the admin policy (economic
loss / abuse), and self-DoS of one's own team on Anthropic. Reliability + cost +
governance divergence between two surfaces over the same models.

**Suggested fix.** Apply the `model.max_output_tokens` ceiling as a real clamp on
the native body's output field before dispatch (translating to the provider's
field: `max_tokens` for Anthropic, `generationConfig.maxOutputTokens` for
Gemini), and clamp what `_reservation_cost` reads too. Absent a per-model
ceiling, impose a global upper bound. Tests: an oversized
`max_tokens`/`maxOutputTokens` is clamped/rejected; a model with
`max_output_tokens` set bounds native output as it does on chat.

### ISSUE-004 — Gemini non-streaming generate_content forks _dispatch and loses the H14 fallback (high)

**Problem.** `_dispatch` (used by chat/responses/embeddings/images and by
`native_messages`) always calls `settle_ok(..., response, latency_ms, request)`,
and `settle_ok` (`usage_meter.py:248-278`) estimates prompt tokens from the
request text when the provider reports no usable usage (the H14 fix).
`generate_content` **doesn't** use `_dispatch`: it hand-reimplements the same
try/except/finally shape but calls
`settle_ok(team_id, api_key_id, model, "native.generate_content", _gemini_usage(response), latency_ms)`
**without the `request` argument** → `request=None` by default → the estimate
branch (`if not _has_tokens(usage) and request is not None`) can never fire on
this path.

**Why it's a problem.** If the upstream Gemini response omits `usageMetadata`
(error responses, safety-blocked completions, or an upstream change),
`_gemini_usage` maps to `{"input_tokens": None, "output_tokens": None}`,
`_has_tokens` is `False`, and with `request=None` the estimate is skipped →
`_parse_usage` bills `prompt=0, completion=0, cost=0.0`, **with no warning or
log**. It reintroduces exactly the zero-cost bug class H14 closed, on the
Gemini non-streaming path alone (streaming always passes `request`, so it's
immune).

**Impact.** Revenue loss / free inference when the provider omits usage data on
the native Gemini non-streaming surface — completely silent. It's also the
clearest instance of "duplication hiding a divergence": `generate_content` is a
fork of `_dispatch` that drifted.

**Suggested fix.** Route `generate_content` through `_dispatch` (as
`native_messages` does via a lambda), or at least pass `request=data` to the
manual `settle_ok`. To reuse `_dispatch` with the native usage view, add an
optional `settle_view: Callable = lambda r: r` and call
`_dispatch(..., settle_view=_gemini_usage)`.

### ISSUE-005 — Gemini passthrough uses the private google-genai `_api_client.async_request` with an open pin (medium)

**Problem.** The native Gemini path calls two **private** (underscore) SDK
methods on `client.aio._api_client`. The signature matches
`google-genai==2.10.0`, but it's non-public API with no stability guarantee, and
the pin is open. The only tests exercising the path
(`tests/completions/conftest.py` `FakeGenaiClient`, used by `tests/native/` and
`tests/conformance/`) hardcode a fake with **exactly the same private shape** the
adapter assumes → they can't catch a release that renames/restructures
`_api_client`.

**Why it's a problem.** A dependency bump (Dependabot active since R7-L40) can
silently break the Gemini passthrough in production with zero CI signal — the
same silent-until-deploy class as R7-L38 (`mlflow>=3.14`). Runtime 500 on the
money/inference path after an upgrade.

**Impact.** Reliability: native Gemini surface breakage after an upgrade, found
only in prod.

**Suggested fix.** Add an upper bound (`>=2.10,<3`) and/or wrap the private call
behind a function with a targeted test that fails with a clear message if the
private method changes presence/signature (against a real `genai.Client` offline,
e.g. an httpx mock transport), so the breakage is in CI not prod. Migrate to a
public raw-request API if one appears.

### ISSUE-006 — Native methods resolve via the "chat.completions" capability slot (medium)

**Problem.** The gateway's four native methods resolve the adapter via the
`"chat.completions"` slot and then call the native method directly, even though
only `AnthropicAdapter`/`VertexAdapter` implement them
(`OpenAIAdapter`/`AzureOpenAIAdapter`/`BedrockAdapter` don't). The capability
matrix (`gateway.py:39-69`), which for every other operation raises
`UnsupportedOperation` (→501) on unsupported providers, is bypassed. The only
thing stopping `gateway.anative_messages(...)` on a Bedrock model is the single
`if model.provider is not Provider.ANTHROPIC: raise ProviderMismatch(...)` check
in the application layer (duplicated for Gemini).

**Why it's a problem.** Defense-in-depth is missing in exactly the layer
(`gateway.py`) that exists to provide it. If the application check is
removed/refactored, or a new caller reaches the gateway directly, the failure is
an unhandled `AttributeError` → opaque 500, not the clean 501 the rest of the
code guarantees (contradicting the file's docstring).

**Impact.** Maintainability / latent-bug trap on future provider additions;
potential opaque 500 instead of 501.

**Suggested fix.** Add `"native.messages"`/`"native.generate_content"` as real
keys in the `_registry`, gate `_resolve` on them, and make the gateway matrix
authoritative (keeping or removing the application check, but so its removal
can't produce a 500).

### ISSUE-007 — Native endpoints emit the OpenAI error envelope, not the provider's (medium)

**Problem.** The native routes live on the same `api_router` whose `DomainError`
handler emits the OpenAI envelope `{"error": {"message","type","code"}}`. A
domain error on `/v1/messages` or `:generateContent` (404/409/402/501…) thus
returns the OpenAI shape, not Anthropic's native
(`{"type":"error","error":{...}}`) or Gemini's
(`{"error":{"code","message","status"}}`). It works today only because the SDKs
derive the exception **type** from the HTTP status, not the body. No test
asserts the body shape on a native route (grep: zero body-shape assertions in
the native/conformance tests).

**Why it's a problem.** The native endpoints' premise ("point the stock SDK and
it works") extends to error handling a sophisticated client may inspect
(`exc.body["error"]["type"]`, retry-reason parsing). Nothing pins whether the
body should stay OpenAI-shaped (a defensible, simpler choice) or become native,
and there's no regression in either direction.

**Impact.** Error ergonomics for native clients (not correctness or security:
the status is right, so retry/backoff on 429/5xx work). Test/contract debt.

**Suggested fix.** Decide and **document** the native surfaces' error-body
contract (OpenAI-shaped is defensible — say so in `docs/native-*.md`) and add at
least one test per surface asserting the body shape on a domain error, not just
the status.

### ISSUE-008 — The prompt estimate ignores Anthropic's top-level `system` field (low)

**Problem.** The native Anthropic body carries the system prompt in a top-level
`system` field (string or list of blocks), separate from `messages`; message
content can include `tool_use`/`tool_result`/image blocks. `_request_text` reads
only textual `messages[].content`, never `system`.

**Why it's a problem.** Under-count of the prompt side of the pessimistic
reservation for native Anthropic calls (e.g. a large system prompt is invisible
to the reservation). Limited impact: the dominant term (`max_tokens`, the output
ceiling) **is** captured for Anthropic, and the final bill always settles on the
provider's authoritative usage — so not independently exploitable at meaningful
scale.

**Impact.** Slightly under-estimated reservation (in-flight throttle accuracy),
not the bill.

**Suggested fix.** Add `system` (string or list of blocks) and other prompt
fields outside `messages` to `_request_text`'s extraction when present.

### ISSUE-009 — Triplicated stream-metering skeleton (DRY debt) (low)

**Problem.** The critical parts (release-exactly-once, shielded settlement,
timeout, error-vs-ok branching) are correctly already factored into
`_finalize_stream_billing` (not duplicated). But the relay loop and its
exception handling are copied three times, differing only in the 4-8 lines that
extract usage/text from the OpenAI, Anthropic and Gemini chunk shapes.

**Why it's a problem.** A future fix to the loop structure (a new cancellation
edge, or a change in what counts as "disconnect vs error") would have to be
carried by hand to three call sites, with nothing guaranteeing they stay in
sync. Not a bug yet (the three are faithful, well-tested mirrors), but a
divergence risk to watch at the next stream-metering change.

**Impact.** Maintainability; future divergence risk.

**Suggested fix** (non-urgent). Extract the shared skeleton into a generic
`_metered_wire_stream(..., extract_usage, extract_text)` and make the three
public methods thin parameter bindings. Given "simplicity first" and that the
triplication is small and consistent, it's a nice-to-have, not a blocker.

### ISSUE-010 — No budget/concurrency test on the native Gemini surface (low)

**Problem.** An over-budget test exists for native Anthropic but not the Gemini
equivalent. Combined with ISSUE-002 (Gemini `$0` reservation), it's exactly the
area where a test would have caught the bug.

**Why it's a problem.** The `admit()` path is shared and tested via Anthropic +
the OpenAI surface, but the absence of a Gemini-native budget/concurrency test
is precisely what let ISSUE-002 slip through.

**Impact.** Coverage; risk of an undetected regression on the Gemini surface.

**Suggested fix.** Add the over-budget test to `test_generate_content.py` (and,
for ISSUE-002, a concurrent-burst test asserting a non-zero reservation and
`BudgetExceeded`).

## Resolution status — REMEDIATED

All findings fixed and merged, except one LOW already covered and one deferred
with rationale. `main` stayed green throughout the remediation (740 tests
passing, `ruff`/`pyrefly`/`pre-commit` clean).

- **001+002+003+004** landed as one cohesive change (#221): reject SDK
  control-kwargs, clamp the output ceiling, per-provider reservations
  centralized in `prepare_native` + `domain/request_policy.py`, and
  `generate_content` re-routed through `_dispatch` (with `settle_view`). The
  **clamped** body is what's actually sent upstream.
- **ISSUE-010** (Gemini budget/concurrency test) is covered by
  `tests/native/test_generate_content.py::test_gemini_reservation_nonzero_gates_concurrent_burst`
  added in #221.
- **ISSUE-009** (DRY refactor of the stream-metering skeleton) is **deferred**:
  the correctness-critical parts (release-once, shielded settlement) are already
  factored into `_finalize_stream_billing`; refactoring the residual relay loop
  over money-critical streaming code is an unfavorable risk/benefit
  (simplicity-first) — flagged, not scheduled.

## Recommended resolution order

1. **ISSUE-001** (critical) — close the credential-override / open-relay vector
   on native Anthropic. Blocking for any production traffic on the native surface.
2. **ISSUE-003** (high) — apply the `max_output_tokens` ceiling + clamp on the
   native path (real unbounded upstream spend; aligns both surfaces).
3. **ISSUE-002** (high) — Gemini-aware reservation (restores the in-flight burst
   guard).
4. **ISSUE-004** (high) — route `generate_content` through `_dispatch`/pass
   `request` (removes the silent $0 billing).
5. **ISSUE-005** (medium) — cap the `google-genai` pin + wrapper/test on the
   private API.
6. **ISSUE-006** (medium) — native capability keys in the gateway matrix.
7. **ISSUE-007** (medium) — decide/document + test the native error-body contract.
8. **ISSUE-008** (low) — include `system` in `_request_text`.
9. **ISSUE-009** (low) — (optional) factor out the stream-metering skeleton.
10. **ISSUE-010** (low) — native Gemini budget/concurrency test (also closes the
    ISSUE-002 gap).

Note: ISSUE-001/002/003/004 are all instances of the same root theme — "verbatim
passthrough" skips the sanitization/clamp/reservation the OpenAI surface applies.
One cohesive change to `prepare_native` (validation + clamp + per-provider
reservation view) closes ISSUE-001, ISSUE-002 and ISSUE-003 together.

## Final assessment

The project is solid and mature overall: seven rounds of hardening made the core
(security, tenancy, money, streaming, migrations, CI) robust, and the *hard* part
of the new native surface — exactly-once settlement, disconnect/cancellation, H24
priming, client lifecycle, settled-billing accuracy — is implemented well and
well tested.

This round's debt is concentrated and single-themed: the **provider-native**
surface, introduced quickly, took "verbatim passthrough" **too literally on the
input and governance side**. The client body is trusted more than it should be
(ISSUE-001, kwargs-injection → credential override, CRITICAL) and the governance
the OpenAI surface applies (output-ceiling clamp, budget reservation, H14
estimate) isn't mirrored on the native path (ISSUE-002/003/004). None requires a
redesign: they're localized fixes bringing the native surface in line with the
existing guards, naturally combinable into a single change on `prepare_native` +
the adapters.

Main improvement areas: (1) treat the native body as **untrusted input** (denylist
the SDK control kwargs, clamp the ceiling, per-provider reservation); (2) reduce
reliance on private SDK APIs with pins and contract tests; (3) make the gateway
capability matrix authoritative for native operations too; (4) close the test
gaps (Gemini budget/concurrency, native error-body shape). With the CRITICAL and
the three HIGHs closed, the native surface is ready for production traffic.
