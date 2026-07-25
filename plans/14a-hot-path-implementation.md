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

## Step 2 — Provider client registry (PR 2, TDD)

Goal: one process-owned, bounded, credential-isolated cache of async SDK
clients so connections are reused across requests.

New files:

- `src/litestar_gateway/infrastructure/llm/client_registry.py`
- `tests/llm/test_client_registry.py`

Design (from plan 14, condensed):

- `ClientKey`: provider, credential id, non-reversible fingerprint of the
  credential material (e.g. HMAC/hash, never the secret), endpoint/base URL,
  API version, region/project, plus every behavior-changing constructor
  option. `repr` must be secret-free.
- `ClientRegistry(capacity: int, ttl_seconds: float)`:
  - `lease(key, factory) -> AsyncContextManager[client]` — hit reuses,
    concurrent misses on one key build exactly one client (per-key lock);
  - LRU + TTL eviction, but an evicted client closes only after its last
    lease is released; close exactly once;
  - `aclose()` for shutdown: closes everything exactly once, waits for
    active leases with a bounded drain timeout;
  - factory failure does not poison the key and leaks nothing;
  - bounded, secret-free metrics: hits, misses, creates, evictions, active
    leases.
- Ownership: constructed in `_build_lifespan()` in
  `src/litestar_gateway/app.py`, injected into `LLMGatewayImpl`
  (`src/litestar_gateway/infrastructure/llm/gateway.py`), closed in the
  lifespan teardown. No module-level global.

Test list (write first, watch them fail):

1. sequential calls with one key → one client, one create;
2. N concurrent first calls with one key → exactly one factory invocation;
3. different provider / credential id / material version / endpoint /
   API version / region → distinct clients, never shared;
4. rotation (same credential id, new material) → new key, new client; old
   client closes only after its in-flight lease releases;
5. eviction at capacity closes the evicted client exactly once, and never
   while leased;
6. TTL expiry behaves like eviction;
7. `aclose()` closes all clients exactly once, twice-idempotent;
8. factory raising → key not poisoned, retry works, nothing to close;
9. cancellation of a leased operation releases the lease without closing
   the shared client;
10. `repr`/metrics/logs contain no credential material (assert on the
    fingerprint being non-reversible and on log capture).

Verify: new tests green, full suite green, `just lint`, `just typecheck`,
coverage ≥80% on the new module.

Exit: registry module merged behind no behavior change (not yet adopted by
any adapter), so the PR is pure addition plus tests.

## Step 3 — Adopt the registry: OpenAI-compatible + Azure (PR 3)

Goal: the benchmark path stops constructing clients per operation.

Touchpoints:

- `src/litestar_gateway/infrastructure/llm/openai_adapter.py` — replace
  `_client`/`_async_client` construct-and-close in `_run`/`_arun` and the
  streaming paths with `registry.lease(...)`; the response stream still
  closes on disconnect, the client does not;
- `src/litestar_gateway/infrastructure/llm/azure_adapter.py` — same, with
  api_version/deployment in the key;
- `src/litestar_gateway/infrastructure/llm/gateway.py` +
  `src/litestar_gateway/app.py` — pass the registry through;
- keep `ResilienceConfig` behavior: timeout/retry kwargs stay identical.

Tests (before implementation):

- adapter-level: two sequential completions reuse one client (spy factory);
- streaming cancellation mid-stream closes the SSE response, not the client;
- rotation mid-traffic: next request uses the new credential;
- existing completions/streaming/upstream-error suites stay green untouched
  (`tests/completions/`, `tests/llm/`).

Measure (the point of the PR):

1. repeat the Step 1 one-worker profile → client-lifecycle frames should
   collapse;
2. run the 3-worker contract 3× → compare medians against frozen v1;
3. report in the PR: RPS, p95, CPU/RPS, RSS, registry hit ratio.

Exit: statistically meaningful improvement (>3%) demonstrated, no regression
in any correctness suite, review blockers from plan 14 all absent.

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
