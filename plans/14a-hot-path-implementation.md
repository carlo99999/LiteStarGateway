# Plan 14a — Hot-path implementation: step-by-step execution

This is the task-level execution companion to
[14-hot-path-throughput.md](14-hot-path-throughput.md). Plan 14 owns the
rationale, the non-negotiable constraints, the acceptance ladder and the review
blockers; this file owns the concrete steps, file touchpoints, commands and
exit criteria. When the two disagree, plan 14 wins.

## Where we are (frozen v1 baseline, 24 July 2026)

Measured with the permanent benchmark contract (`just load-contract`), three
runs, medians retained:

- non-streaming: 100.0 / 164.6 / 165.9 successful RPS at 100/200/300 offered;
- streaming: 99.7 / 152.1 / 150.1 successful RPS at 100/200/300 offered;
- honest saturated ceilings: **~165 RPS non-streaming, ~152 RPS streaming on
  3 workers / 3 CPU** (~55 and ~50 RPS per core);
- target: 300 completed RPS in both modes on the same footprint — roughly
  **2× per-core efficiency**.

The dominant per-request waste hypothesis: every adapter operation constructs
and closes its own SDK client, so every request pays client construction, an
httpx pool setup, and a TCP+TLS handshake to the provider
(`openai_adapter.py:215 _async_client`, the repeated `AsyncAnthropic(...)`
blocks in `anthropic_adapter.py`, `vertex_adapter.py:207 _client`).

## Measurement protocol (used by every step)

All before/after claims use the same commands on the same host:

```bash
# 3-worker deployment measurement (the baseline-v1 conditions):
LOAD_STAGES="100,200,300" LOAD_DURATION_SECONDS=30 \
  LOAD_PROFILE_POLICY=diagnostic just load-contract

# 1-worker code-efficiency measurement:
UVICORN_WORKERS=1 LOAD_STAGES="40,60,80" LOAD_DURATION_SECONDS=30 \
  LOAD_PROFILE_POLICY=diagnostic just load-contract
```

Rules:

- compare successful completed RPS, p50/p95, CPU/RPS and RSS against the
  frozen v1 medians in plan 14;
- one run is a smoke; three runs with the median retained is a claim;
- a gain within run-to-run noise (≤3%) is rejected;
- never change benchmark parameters and code in the same PR.

## Step 0 — Housekeeping (immediate)

1. Commit the frozen-baseline plan update
   (`plans/14-hot-path-throughput.md`) and the refreshed graph.
2. Confirm `main` is green (`just lint`, `just typecheck`, full suite).

Exit: baseline v1 is in git history before any optimization lands.

## Step 1 — Before-profile on one worker (PR #348, evidence only) — ✅ done

Goal: prove (or refute) that client lifecycle is the dominant per-request
cost, and produce the "before" flamegraph every later PR compares against.

Implementation note: instead of a `profile` uv dependency group, py-spy was
added via a dedicated Docker build stage (`profile`, extending `runtime`) with
`setcap cap_sys_ptrace+eip` applied to the py-spy binary at build time. This
lets the non-root `app` user invoke py-spy directly against a sibling process
without `docker exec --user root`, and keeps py-spy completely out of the
production image (the `runtime` target is untouched). The compose overlay
`docker-compose.profile.yml` builds the `app` service from the `profile`
target and adds `cap_add: [SYS_PTRACE]`; `scripts/benchmark-compose.sh` picks
it up automatically when `PROFILE=1` is set.

Tasks completed:

1. `Dockerfile`: added the `profile` stage.
2. `docker-compose.profile.yml`: overlay, opt-in via `PROFILE=1`.
3. Ran the 1-worker contract (`PROFILE=1 UVICORN_WORKERS=1
   ./scripts/benchmark-compose.sh up`) at a saturating chat stage (80 RPS
   offered) and captured 55 s of samples with `py-spy record --pid 1
   --format raw` (both an SVG flamegraph and the raw folded-stack format were
   produced for the chat stage).
4. Attempted the same for a streaming stage; the capture was abandoned
   partway through (see "Environment caveat" below) once the code-level
   confirmation below made a second capture unnecessary for the decision
   gate.
5. Artifacts stored under `load-results/20260725-032657-profile/`
   (gitignored, as with all `load-results/` content).

### Result: the client-lifecycle hypothesis is confirmed, decisively

Of 5,589 stack samples collected during the saturating chat-80 stage:

- **53.3%** of *all* CPU self-time samples landed in a single frame:
  `ssl.py:717 create_default_context`;
- **59.7%** of samples had `httpx` somewhere on the stack;
- **54.2%** had `ssl` somewhere on the stack;
- **61.3%** had `__init__` somewhere on the stack (client construction);
- every other named hot path — SQLAlchemy ORM hydration, asyncpg execution,
  greenlet bridging, JSON decoding — was **under 0.5%** each.

The full call stack for the dominant frame is unambiguous:

```text
completions.py:62 chat_completions
  → completion_service.py:675 chat_completion
  → completion_service.py:217 _dispatch
  → gateway.py:94 achat_completion
  → errors.py:158 arun_translated
  → openai_adapter.py:126 achat_completion
  → openai_adapter.py:112 _arun
  → openai_adapter.py:216 _async_client        ← client constructed here
  → openai/_client.py:860 __init__
  → openai/_base_client.py:1519/1426 __init__
  → httpx/_client.py:1402 __init__
  → httpx/_client.py:1445 _init_transport
  → httpx/_transports/default.py:297 __init__
  → httpx/_config.py:40 create_ssl_context
  → ssl.py:717 create_default_context          ← 53.3% of all CPU samples
```

`ssl.create_default_context()` reads and parses the OS CA bundle from disk
and is documented as expensive precisely because it is meant to be called
once and reused — the gateway calls it on essentially every request, because
`OpenAICompatibleAdapter._async_client()` (`openai_adapter.py:216`)
constructs a brand-new `AsyncOpenAI` → `httpx.AsyncClient` → transport → SSL
context for every single non-streaming call, and `_run`/`_arun` close it
immediately after (`openai_adapter.py:107,116`).

The streaming path (`astream_chat_completion`, `openai_adapter.py:143`) calls
the exact same `_async_client()` before opening the SSE stream, so this cost
applies identically there — confirmed by code inspection rather than a
second full profile capture (see caveat below).

### Environment caveat: ptrace over Docker Desktop's VM is slow

py-spy's ptrace-based sampling stops the target process on every sample. On
this host (macOS + Docker Desktop, i.e. a virtualized Linux VM), each
ptrace stop/read/continue cycle crosses the virtualization boundary and is
far slower than on bare-metal Linux. Effects observed:

- at the default 100 Hz rate under ~90 concurrent users, py-spy fell up to
  86 seconds behind a 55-second recording window;
- the achieved RPS *during profiling* (20–47 RPS) is depressed by the
  profiler's own overhead and must not be compared against the frozen v1
  baseline (56 RPS unprofiled) — profiling perturbs the measurement, which is
  expected and is why Step 1 is "evidence, no PR to production code";
- a second streaming capture at a reduced rate (40 Hz) still took several
  minutes of wall time for a nominal 45-second recording and was killed once
  the chat-mode result plus the shared `_async_client()` call site made
  further data collection unnecessary for the decision gate.

This is a profiling-tool/host limitation, not a gateway property — it does
not affect the validity of the self-time percentages above (relative
proportions among frames actually sampled), only the wall-clock time needed
to collect them and the achieved-RPS number during collection. On bare-metal
Linux (e.g. CI or a cloud runner) this would be expected to run close to the
nominal duration.

Decision gate: **client lifecycle is the dominant per-request cost.**
Proceed to Step 2 (the client registry). No fallback to Step 5 is needed.

Exit: flamegraphs and raw profiles archived under
`load-results/20260725-032657-profile/`; decision recorded above.

## Step 2 — Provider client registry (PR #349, TDD) — ✅ done

Implemented as designed below. `ClientRegistry` (bounded LRU+TTL, per-key
leasing, double-checked-locking construction, secret-free `ClientKey` via a
one-way `fingerprint_material()` hash) lives in
`src/litestar_gateway/infrastructure/llm/client_registry.py`, with 16 tests in
`tests/llm/test_client_registry.py` (98% coverage) covering every item in the
mandatory list below plus two extra races (a genuine in-lock double-check hit,
forced via controlled interleaving since `asyncio.Lock.acquire()` doesn't
yield when uncontended; and a failing `close()` callback, which must log and
never propagate).

Ownership wiring (no adapter touched, no behavior change — verified by the
full 1,430-test suite staying green): `LLMGatewayImpl` now owns one
`ClientRegistry` instance (`gateway.py`), exposes `async def aclose()`, and
`app.py`'s composition root calls it from a new lifespan manager
(`_make_llm_gateway_lifespan`, entered first so it unwinds last — provider
clients stay alive until every other lifespan manager has finished). No
adapter leases from it yet; that is Step 3.

## Step 3 — Adopt the registry: OpenAI-compatible + Azure (PR 4) — ✅ done

Implemented as designed: `openai_adapter.py`'s `_arun`/`astream_chat_completion`/
`astream_responses` now go through `_leased_async_client()` (an
`@asynccontextmanager` that leases from the registry when one is wired in, or
falls back to the exact old construct-and-close behavior otherwise — so
standalone-constructed adapters in tests are unaffected). Streaming leases the
client for the whole generator lifetime; cancellation/disconnect unwinds the
`async with`, releasing the lease without force-closing a client another
request still holds. `azure_adapter.py` mirrors this, keying on the same
resolved kwargs (`api_key`, `azure_endpoint`, `api_version`) used to construct
the client, so the two can never drift out of sync. `gateway.py` passes
`self._client_registry` into the shared `OpenAIAdapter` instance (which
Databricks reuses via `ChatToResponsesAdapter`, adopting it for free) and into
`AzureOpenAIAdapter`. Sync methods (`_run`, `chat_completion`, etc.) are
untouched — they aren't on the profiled async hot path.

New adapter-level tests in `tests/llm/test_openai_adapter_registry.py`:
sequential reuse, credential rotation producing a distinct client, and stream
cancellation releasing the lease without closing a client held by a concurrent
request — all against a real `ClientRegistry` with a fake SDK client (no
network, no real `AsyncOpenAI` construction).

### An unplanned but necessary fix: connection-pool sizing

The first full 3-worker measurement surfaced a real regression: streaming at
200 RPS offered dropped to 113 achieved with 9% failures and 10s+ TTFT (worse
than the frozen baseline's 152 RPS *without* the registry). Root cause: a
registry-leased client is now shared by every concurrent request for one
credential, but httpx's own default pool (`max_connections=100`) was sized for
one request per client, not hundreds sharing one. Fixed by adding
`ResilienceConfig.async_client_kwargs` (`resilience.py`), which supplies an
explicit `httpx.AsyncClient` with a generous bounded pool
(`max_connections=1000`, `max_keepalive_connections=100`) — confirmed the
OpenAI SDK's `close()` always closes the underlying httpx client even when
supplied by the caller, so this doesn't change close semantics. `httpx` was
promoted from a transitive to a direct dependency since the code now imports
it. Four new tests in `tests/llm/test_resilience_config.py`. The exact,
deployment-wide pool sizing is still Step 6's job; this is a safe, generous
interim default.

### Measured result (3 runs, medians; 3 workers / 3 CPU, same conditions as

the frozen v1 baseline)

| Mode | Offered | v1 baseline | After Step 3 | Change |
|---|---:|---:|---:|---:|
| non-streaming | 100 | 100.0 | 100.2 | +0.2% |
| non-streaming | 200 | 164.6 | 199.8 | **+21.4%** |
| non-streaming | 300 | 165.9 | 245.3 | **+47.9%** |
| streaming | 100 | 99.7 | 99.5 | −0.2% |
| streaming | 200 | 152.1 | 199.2 | **+31.0%** |
| streaming | 300 | 150.1 | 151.8 | +1.1% (0% → 11% failures) |

At 100 RPS both modes were already unsaturated, so no change is expected. At
200 RPS — solidly saturated in the v1 baseline — both modes now sustain
essentially the full offered load. At 300 RPS non-streaming still doesn't
pass its p95 gate but throughput jumped ~48%; 300 RPS streaming lands close to
the old ceiling but now with real failures (~11%, up from the baseline's
0–5%), meaning **the bottleneck has moved**: client-lifecycle waste no longer
dominates, so something else caps throughput starting around 250-300 RPS.
That's Step 5's job to find, not this PR's.

Exit: statistically meaningful improvement demonstrated (far beyond the >3%
noise floor) at every previously-saturated stage; zero regressions in the
full 1,438-test suite; review blockers from plan 14 all absent (bounded pool,
no cross-credential sharing, rotation-safe, cancellation-safe).

## Step 4 — Adopt the registry: Anthropic + Vertex, assess the rest (PR 5) — ✅ done

1. `anthropic_adapter.py`: all four async construct/close sites
   (`achat_completion`, `astream_chat_completion`, `anative_messages`,
   `astream_native_messages`) now go through `_leased_async_client()`, same
   pattern as Step 3's `OpenAICompatibleAdapter`.
2. `vertex_adapter.py`: reuse via a new `_async_client()` (distinct from the
   existing `_client()` used by the sync methods), keyed by
   `vertex_project`/`vertex_location`/`vertex_credentials` (the service-account
   JSON — a credential-material dimension, same treatment as an API key).
   **Compatibility wrinkle**: `genai.Client`'s async close lives at
   `client.aio.aclose()`, not `client.close()` (unlike OpenAI/Anthropic) — one
   `ClientRegistry` instance has exactly one close callback for everything it
   holds, so Vertex gets its **own** registry (`LLMGatewayImpl` now owns two:
   `_client_registry` for OpenAI/Azure/Anthropic, `_vertex_client_registry` for
   Vertex), both closed on gateway shutdown. Also applied the Step 3
   connection-pool fix here (`HttpOptions(httpx_async_client=...)`, confirmed
   `google-genai` accepts a caller-supplied async httpx client the same way
   OpenAI/Anthropic do).
3. **Bedrock — assessed, deliberately not adopted.** `bedrock_adapter.py`
   already reuses one boto3 client's *connections* correctly (a shared
   `ThreadPoolExecutor` runs blocking calls off the event loop); AWS
   documents boto3 clients as safe for concurrent calls from multiple
   threads, so reuse is safe in principle. Deferred anyway: it isn't
   exercised by the deterministic benchmark contract (no before/after
   throughput evidence either way), and leasing a client that's used from a
   *worker thread* rather than the event loop needs its own design
   (the lease must stay held across the thread hop). Revisit only if a future
   profile shows Bedrock client construction is materially expensive — no
   evidence of that exists yet.
4. Databricks (OpenAI-compatible): confirmed free — `gateway.py` wraps the
   *same* `OpenAIAdapter` instance (with the registry already wired in from
   Step 3) via `ChatToResponsesAdapter`, so it was never a separate adoption.

New adapter-level tests: `tests/llm/test_anthropic_adapter_registry.py` and
`tests/llm/test_vertex_adapter_registry.py`, mirroring Step 3's OpenAI tests
(sequential reuse, rotation produces a distinct client, stream cancellation
releases the lease without closing a client held by a concurrent request).

Two pre-existing Vertex integration tests
(`tests/completions/test_responses_api.py::test_vertex_chat_translation`,
`tests/completions/test_vertex_tools_integration.py::...preserves_signature_and_billing`)
asserted the *old* "closed after every call" behavior as a correctness
invariant; updated both to assert the new cached-and-reused behavior instead
— this is the intended behavior change, not a regression.

No throughput measurement for this step: the deterministic benchmark contract
only exercises OpenAI-compatible traffic (the mock speaks that protocol), so
Anthropic/Vertex have no comparable before/after numbers. Validated instead
by the adapter-level tests above and the full suite staying green.

Exit: every adapter either leases from the registry or has a written
justification (Bedrock); conformance + native endpoint suites green; full
suite 1,444 passed, 6 skipped, zero regressions beyond the two intentionally
updated assertions.

## Step 5 — Re-profile and rank the next hotspot (evidence, then PR 5) — ✅ evidence gathered

Re-ran the Step 1 profile on 1 worker: a saturating chat-90 stage (90 RPS
offered, 60 s steady), captured with `py-spy record --pid 1 --format raw`
after the Step 2–4 registry adoption. Achieved 77.9 RPS at p95 1.6 s — close
to Step 3's dedicated 80-RPS measurement (78.4 RPS, p95 920 ms), confirming
the 1-worker ceiling is now around 78–80 RPS, up from the frozen v1
baseline's ~56 RPS (+ ~40%).

### Result: no single dominant hotspot remains

Of 2,304 samples, the two largest leaf frames are profiling artifacts, not
real work: `_worker (concurrent/futures/thread.py:116)` (13.8%) is an idle
thread-pool worker blocked in its queue wait (bare stack, nothing calls into
it), and `run (asyncio/runners.py:128)` (13.2%) is the main thread idling in
the event loop's I/O wait. Neither represents CPU-bound work.

Excluding those, the real leaf frames are all small and roughly comparable
in size — none exceeds 1.0% of samples:

| Frame | Self-time |
|---|---:|
| `decode_json` (msgspec) | 1.0% |
| `__do_execute` (asyncpg) | 0.7% |
| `execute` (asyncpg/connection.py) | 0.7% |
| `validate` (h11) | 0.7% |
| SQLAlchemy ORM session/state (combined) | ~1.2% |
| `greenlet_spawn` (sync/async bridge) | 0.4% |
| Litestar middleware `wrapped_call` | 0.4% |

Keyword co-occurrence (any frame in the stack, not self-time) shows
`sqlalchemy` (37.0%), `rate_limit` (39.9%) and `connect` (22.1%) touch a
large share of requests, as expected for a gateway that does auth, rate
limiting, model/credential resolution and DB-backed billing on every call —
but no individual operation dominates the way SSL context creation did
before Step 2.

Artifacts: `load-results/step5-profile/chat-90rps-1worker-post-registry.raw`
(gitignored).

### Decision: stop here, not attack a diffuse tail

The plan's candidate list (metadata-lookup caching, credential-decrypt
caching, rate-limit/budget pipelining, serialization, logging) is still
individually plausible, but every candidate now represents a low-single-digit
percentage of total time, not a step-change. Implementing any one of them
safely requires real complexity the plan itself calls out as non-negotiable
— a metadata cache "must be bounded, tenant-safe and invalidated for
credential rotation, model disablement, revoked grants, API keys and policy
changes" — which is a meaningful, separately-reviewable change for a
low-single-digit gain, not a quick win.

Given the code-efficiency gate is "≥100 non-streaming RPS/core" and the
registry alone reached ~78–80 (Step 2–4 closed roughly 80% of the gap from
the ~56 RPS starting point), further gains here are real but incremental.
This plan treats Step 5 as **evidence-complete**: the dominant cost is gone,
remaining costs are diffuse and individually small, and attacking them is
future work rather than part of this initial throughput push. Proceeding to
Step 6 to measure the actual 3-worker/3-CPU ceiling this unlocks and report
the honest final numbers, per plan 14's "Definition of done" outcome 2 (all
profile-proven safe optimizations delivered + honest ceilings recorded) if
outcome 1 (300/300) isn't reached.

Exit: 1-worker ceiling improved from ~56 to ~78–80 RPS (code-efficiency gate
not fully met, but the measured, validated majority of the gap is closed);
no further single-hotspot PR is justified by the evidence.

## Step 6 — Deployment tuning and final acceptance — ✅ measured, outcome 2

Ran the plan 14 acceptance ladder at the mandated 60 s minimum steady window
(not the 30 s used in the Step 3/Step 5 protocol for faster iteration) — 3
runs, 3 workers / 3 CPU / 4 GiB, medians retained:

| Mode | Offered | v1 baseline (30 s) | After registry (60 s, via host proxy) | Change |
|---|---:|---:|---:|---:|
| non-streaming | 100 | 100.0 | 100.0 | +0.0% |
| non-streaming | 200 | 164.6 | 199.9 | **+21.5%** |
| non-streaming | 300 | 165.9 | 247.2 | **+49.0%** |
| streaming | 100 | 99.7 | 99.5 | −0.2% |
| streaming | 200 | 152.1 (0% fail) | 162.7 (5.5–6.5% fail)¹ | see resolution below |
| streaming | 300 | 150.1 (0–5.2% fail) | 149.3 (14–15% fail)¹ | see resolution below |

¹ The 60 s streaming failures were later proven to be a **measurement-
infrastructure artifact** (Docker Desktop's host→container port proxy), not
gateway behavior — full investigation below. Measured in-network (no proxy in
the path), streaming-200 sustains **199.5 RPS, 0.000% failures, p95 270 ms,
TTFT p95 230 ms** for the full 105 s window: a clean gate PASS.

### An honest finding the 30 s protocol missed — resolved: proxy artifact

What we initially observed: streaming at 200 RPS offered passed cleanly at
30 s windows (Step 3) but showed a hard failure onset at **exactly t≈60 s**
of sustained load — reproducible across every run, always the same second,
with `status 0` (no HTTP response at all) client-side errors, temporary
recovery, then re-collapse in a rough 30–45 s cycle. Initially recorded here
as a suspected registry-induced regression. That attribution was wrong, and
the investigation that established the real cause is worth recording:

1. **Seven server-side hypotheses eliminated by direct experiment**: gateway
   `REQUEST_TIMEOUT` (lowered to 20 s — onset stayed at 60 s), DB pool
   exhaustion (production-size pool — no change), the usage reconciler's
   60-second loop (disabled entirely — no change), container file-descriptor
   limits (1M, nowhere near), Locust's own 60 s `network_timeout`/
   `connection_timeout` (raised to 120 s — onset **stayed at 60 s**), CPU
   hotspots (three per-worker py-spy profiles during the collapse: nothing),
   and the upstream mock (its own metrics showed ≤30 concurrent requests and
   *zero* new arrivals during the collapse — requests died before reaching
   it).
2. **The pre-registry control**: the identical 90 s diagnostic against the
   pre-registry commit (`568f522`) showed **zero failures** — it never went
   fast enough to trip the real limit (~150 RPS of multi-second queued
   streams vs. ~200 RPS of ~200 ms streams after the registry).
3. **The smoking gun**: sampling host-side TCP socket states during a run.
   Healthy phase: ~150 ESTABLISHED keep-alive connections, TIME_WAIT ≈ 0.
   At t≈60 s: ESTABLISHED jumps to ~1,500 and TIME_WAIT explodes from 0 to
   **12,282** — mass connection churn on the macOS-host side of Docker
   Desktop's port forward, exactly at failure onset.
4. **The decisive isolation**: the same load, same duration, same gateway,
   but with Locust running **inside the Docker network** (container→container,
   no host proxy): **199.5 RPS, 0.000% failures, p95 270 ms for the full
   window.** The collapse exists only when Docker Desktop's host→VM port
   proxy is in the path.

Verdict: the registry did not regress streaming — it made the gateway fast
enough to exceed what Docker Desktop's macOS port-forwarding proxy can
sustain for long-duration high-churn streaming. The "regression" lived in
the measurement path, not the system under test. The pre-registry gateway
never hit it because it was too slow to.

Consequence for the benchmark contract: on macOS/Docker Desktop hosts,
sustained streaming acceptance runs **must run the load generator inside the
Docker network** (or on a Linux host with native networking). A follow-up to
containerize the load generator in `docker-compose.benchmark.yml` is the
natural next benchmark-contract improvement; until then, host-proxy streaming
numbers at ≥60 s are not valid evidence.

### Pool sizing reviewed, not changed

Production defaults (`DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=10`) give 15
connections per worker, 45 at 3 workers — within a typical single-deployment
Postgres `max_connections` (often 100+ by default) but worth stating
explicitly for operators running multiple replicas or a smaller Postgres
tier. The benchmark contract itself runs with `DB_MAX_OVERFLOW=0` (15 total)
as a deliberately bounded test configuration. Provider connection pools were
already sized in Step 3 (`ResilienceConfig.async_client_kwargs`,
1000 max / 100 keepalive) — no further change made here, and the streaming
investigation above confirmed pool size was not implicated (the failures
never reached the gateway at all).

uvloop/httptools were not evaluated: nothing in the Step 5 profile pointed at
event-loop or HTTP-parser overhead as a leading cost.

Graceful shutdown and rotation-under-load were validated indirectly (the
full 1,444-test suite exercises the app lifespan, including the registry's
`aclose()`, on every test run) but not under live 3-worker traffic — a real
gap, noted rather than closed, given time budget.

### Outcome

Plan 14's "Definition of done" is met via **outcome 2**: all profile-proven
safe optimizations from Steps 2–4 are delivered (client-lifecycle waste
eliminated) and the honest ceilings are recorded above. Non-streaming
improved substantially and durably (+21.5% at 200, +49.0% at 300 offered).
Streaming, once the measurement-path artifact was isolated and removed,
**passes its 200 RPS gate cleanly at the full acceptance duration**
(199.5 RPS, 0% failures, p95 270 ms, TTFT p95 230 ms, in-network). The
300 RPS target in both modes remains unmet on 3 CPU (non-streaming honest
ceiling ~247, streaming ≥200 with the in-network ceiling not yet probed
beyond 200), so outcome 1 is still not claimed; the remaining follow-ups are
containerizing the load generator in the benchmark contract and re-probing
the true in-network streaming ceiling.

## PR sequence summary

| PR | Content | Gate it must pass |
|----|---------|-------------------|
| 0 | commit frozen baseline (docs only) | none |
| 2 | client registry, unadopted | full suite + coverage on new module |
| 3 | OpenAI/Azure adoption | >3% median gain vs v1, zero regressions |
| 4 | Anthropic/Vertex (+Bedrock/Databricks assessment) | conformance green |
| 5.x | one measured hotspot each | before/after profile + contract run |
| 6 | pools, shutdown, acceptance ladder | plan 14 acceptance ladder |

Every PR carries: baseline numbers, after numbers, raw-report directory,
test plan, and the explicit statement of preserved invariants (auth, rate
limits, budgets, reservations, settlement, routing, tenant isolation).

## Risks / open questions

- py-spy inside the production image needs `SYS_PTRACE`; keep the overlay
  out of the production compose files.
- Azure/Vertex constructor kwargs must be enumerated exhaustively for the
  key; a missed behavior-changing option is a review blocker.
- Bedrock thread-safety may forbid reuse — acceptable outcome, record it.
- The streaming ceiling (~152 RPS) trails non-streaming (~165); if Step 3
  narrows the gap less than expected, SSE serialization moves up Step 5's
  ranking.
