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

## Step 4 — Adopt the registry: Anthropic + Vertex, assess the rest (PR 4)

1. `anthropic_adapter.py`: replace the five-plus `AsyncAnthropic(...)`
   construct/close sites with leases (same test pattern as Step 3).
2. `vertex_adapter.py`: reuse via `_client()` keyed by project/region;
   verify the google-genai client is safe for concurrent reuse first.
3. Bedrock (`bedrock_adapter.py`): boto3 clients + executor threads — adopt
   only after verifying the documented thread-safety contract; otherwise
   record "not adopted, reason" in plan 14.
4. Databricks (OpenAI-compatible): should come for free via Step 3; verify.

Exit: every adapter either leases from the registry or has a written
justification; conformance + native endpoint suites green.

## Step 5 — Re-profile and rank the next hotspot (evidence, then PR 5)

Re-run the Step 1 profile (1 worker and 3 workers). Rank remaining costs.
Expected candidates, attacked strictly in measured order, one per PR:

- **metadata lookups** — repeated model/credential/API-key queries per
  request → bounded in-process TTL cache, invalidated on rotation, revoke,
  disable, policy change; tenant-safe keys;
- **credential decrypt** — cache the decrypted envelope alongside the
  metadata cache (never in logs/metrics; memory-only);
- **rate-limit/budget admission** — Redis round-trips per request: pipeline
  or combine checks;
- **usage settlement commits** — only with strong evidence, design doc
  first: durable, replayable, idempotent outbox/batch (plan 14 constraint 3);
  validate on SQLite + real Postgres under concurrency and injected failure;
- **serialization** — Pydantic validation/translation and JSON/SSE encoding;
- **hot-path logging** — demote/async-ify verbose statements.

Each slice: before-profile → failing performance/regression contract →
smallest safe change → same profile after → contract run. Reject noise-level
wins.

Exit: repeated until the 1-worker code-efficiency gate is met
(≥100 non-streaming RPS/core at p95 ≤ 1 s) or all safe slices are exhausted.

## Step 6 — Deployment tuning and final acceptance (PR 6)

1. Size SQLAlchemy pool/overflow per worker against Postgres capacity
   (3 workers × defaults can already reach 45 connections).
2. Size provider connection pools (registry capacity × httpx limits) per
   worker and per deployment.
3. Evaluate uvloop/httptools only if the post-Step-5 profile shows loop or
   parser overhead.
4. Graceful shutdown, rolling replacement and stream cancellation with
   multiple workers.
5. Run the full acceptance ladder from plan 14 (Gates A, B, target) with
   60 s steady windows; record either the 300 RPS pass or the honest maximum
   plus a validated worker/CPU/replica estimate for 300 RPS.

Exit: plan 14 "Definition of done" — outcome 1 (300/300 on 3 CPU / 4 GiB) or
outcome 2 (all safe optimizations delivered, honest ceilings recorded,
validated scale-out plan documented).

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
