# Plan 14 — Gateway hot-path throughput

This plan supersedes the first single-worker draft. It incorporates the
deterministic 1-worker/3-worker measurements from 24 July 2026 and turns the
remaining work into an evidence-driven code optimization plan.

## Outcome and current status

The worker-scaling question is resolved:

- one Uvicorn worker uses approximately one CPU, even when the container has
  three CPUs available;
- three workers use all three CPUs and raise non-streaming throughput from
  roughly 56 RPS to roughly 150 RPS;
- the 3-worker gateway passes 100 RPS comfortably, but offering 200 or 300 RPS
  only creates a queue and increases latency;
- streaming is more expensive and reaches a lower ceiling of roughly
  120–127 completed streams/s;
- therefore the next material work is in the gateway hot path. Adding workers
  alone does not meet the 300 RPS target on the desired 3 CPU footprint.

Runtime support for configurable Uvicorn workers is complete. Profiling and
code-level optimization have not started.

## Reproducible measured baseline

All deterministic runs used:

- the production Docker image, PostgreSQL and Redis;
- MLflow disabled and the UI enabled;
- a 3 CPU / 4 GiB backend limit;
- `UVICORN_WORKERS=3`, except for the explicit 1-worker control;
- an authenticated `POST /v1/chat/completions` request through the complete
  gateway path;
- a local OpenAI-compatible mock with fixed 50 ms upstream latency;
- one upstream attempt, eight maximum output tokens and no provider cost;
- independent 30-second steady stages after a 10-second ramp and 5-second
  settle period.

### Worker-scaling control

| Workers | Offered load | Achieved | Failures | p50 | p95 | Gateway CPU |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 100 RPS | 56.41 RPS | 0% | 2.2 s | 2.5 s | 101–102% |
| 3 | 100 RPS | 99.93 RPS | 0% | 220 ms | 320 ms | ~222–251% |

The saturated 3-worker ceiling of roughly 151 RPS is 2.7× the 1-worker ceiling
of roughly 56 RPS. This confirms that the earlier 1-worker result was CPU-bound.
The remaining difference from ideal linear scaling is part of the hot-path
investigation, not evidence of an upstream limit.

### Three-worker saturation

| Mode | Offered load | Achieved | Failures | p50 | p95 | Result |
|---|---:|---:|---:|---:|---:|---|
| non-streaming | 100 RPS | 99.93 RPS | 0% | 220 ms | 320 ms | pass |
| non-streaming | 200 RPS | 151.42 RPS | 0% | 1.6 s | 2.3 s | saturated |
| non-streaming | 300 RPS | 148.36 RPS | 0% | 2.4 s | 3.3 s | saturated |
| streaming | 100 RPS | 100.10 RPS | 0% | 350 ms | 570 ms | pass |
| streaming | 200 RPS | 127.32 RPS | 0% | 1.7 s | 3.4 s | saturated |
| streaming | 300 RPS | 120.18 RPS | 0% | 2.9 s | 6.4 s | saturated |

At the saturated stages the container consumed approximately 300–305% CPU.
Memory remained within the 4 GiB limit, rising from roughly 1.37 GiB to an
observed peak of roughly 1.49 GiB. Streaming TTFT p95 was 540 ms at 100 RPS and
4.6 s at the overloaded 300 RPS stage.

Raw local reports:

- `load-results/20260724-160919` — non-streaming 100 RPS;
- `load-results/20260724-161005` — non-streaming 200 RPS;
- `load-results/20260724-161051` — non-streaming 300 RPS;
- `load-results/20260724-161136` — streaming 100 RPS;
- `load-results/20260724-161222` — streaming 200 RPS;
- `load-results/20260724-161307` — streaming 300 RPS.

These reports are local evidence and are intentionally gitignored.

### Live-provider control

A separate run against `gpt-5.4-mini` sustained the requested 10, 20 and 30 RPS
stages in both modes with stable sub-1.3-second p95 latency. One non-streaming
call received an upstream 502. CPU stayed around one core because provider wait
time dominated. This control shows that the gateway can proxy the tested live
load, but it cannot establish the gateway ceiling: provider latency and quotas
make the deterministic local mock the acceptance benchmark.

## What the measurements mean

The current non-streaming ceiling is approximately 50 successful RPS per fully
used CPU; streaming is approximately 40 RPS per CPU. Reaching 300 RPS on three
CPUs requires roughly doubling per-core non-streaming throughput and improving
streaming even further.

At current efficiency, six CPU-bound workers/cores would be the naive
non-streaming estimate for 300 RPS, and streaming would require more. That is
only a capacity hypothesis. It is not the desired solution and must not be
presented as validated until measured.

The first code hypothesis is provider client lifecycle. OpenAI, Azure, Anthropic
and Vertex adapters currently construct and close SDK clients—and therefore
their HTTP connection pools—per operation. The process-lifetime
`LLMGatewayImpl` assembled in `app.py` provides a natural owner for a bounded
client registry. This is still a hypothesis: profiles must prove its CPU and
wall-time contribution before and after the change.

Other credible costs are model/credential lookup, decryption and parsing,
rate-limit and budget checks, usage settlement commits, request/response
validation and translation, serialization, logging, and event-loop/DB-pool
contention.

With three workers, process-local resources are multiplied. In particular, the
default SQLAlchemy settings permit `pool_size=5` plus `max_overflow=10` per
worker, or up to 45 database connections for three workers. Provider-client and
database pools must therefore be sized as a per-process and total deployment
budget.

## Non-negotiable constraints

1. Use TDD for every implementation slice: failing lifecycle/performance
   contract, minimal implementation, refactor, then full regression gate.
2. Preserve authentication, authorization, per-IP/team/key rate limits, budget
   admission, in-flight reservations, usage settlement, routing, retries,
   timeouts, error translation and tenant isolation.
3. Never improve throughput by skipping or weakening authoritative billing
   writes. Any batching/outbox proposal must be durable, replayable, idempotent
   and crash-safe.
4. Never share provider clients across credentials or incompatible endpoint
   configuration. Never expose credentials or secret-derived identifiers in
   logs, metrics, errors, cache keys outside the process or benchmark artifacts.
5. Every cache and pool must be bounded, concurrency-safe and closed
   deterministically. Credential rotation must take effect without restarting a
   worker.
6. A cancelled stream must release its stream, reservation and request resources
   without closing a shared client still leased by another request.
7. Keep domain and application layers independent of provider SDK, Litestar,
   SQLAlchemy and other infrastructure details.
8. No live-provider performance calls in CI. Tests and performance contracts
   use deterministic fakes.
9. Report completed successful requests/streams, not merely accepted requests.
   Never hide failures or claim offered RPS as achieved RPS.

## Phase 0 — Make the benchmark a reliable engineering gate

The current local mock proved the bottleneck but was temporary. Make the
experiment repeatable before changing production behavior.

Tasks:

- add a deterministic OpenAI-compatible mock for ordinary and SSE chat
  completions, with configurable TTFT, chunk/total latency, status failures and
  valid usage;
- give chat and chat-stream explicit independent selectors so a failed mode or
  stage never prevents collecting the requested comparison;
- allow a fail-fast acceptance mode and an explicit diagnostic mode that
  continues through later stages;
- align the process exit code with the configured failure threshold instead of
  allowing Locust's default “any failure” policy to disagree with the custom
  gate;
- add mode-specific p95 and TTFT limits;
- compute concurrency from expected latency with documented headroom and verify
  the load generator is not saturated;
- record completed RPS, status/cause failures, p50/p95/p99, streaming TTFT,
  event-loop lag, gateway CPU/RSS, DB pool wait, Redis timing and provider timing;
- retain exact commit, image, worker count, CPU/memory limit, command and report
  location;
- use at least 60 seconds of steady state for acceptance runs.

Phase 0 exit criteria:

- the six baseline stages above can be reproduced within ±10%;
- ordinary and streaming runs can be selected independently;
- a synthetic injected failure proves the configured threshold and exit code
  agree;
- no real provider credential or benchmark model is needed.

## Phase 1 — Profile and fix provider-client lifecycle

Profile CPU, allocations, event-loop lag and wall time on one worker first, then
on three workers. Instrument without logging secrets. Start with:

- `src/litestar_gateway/infrastructure/llm/openai_adapter.py`;
- `src/litestar_gateway/infrastructure/llm/azure_adapter.py`;
- `src/litestar_gateway/infrastructure/llm/anthropic_adapter.py`;
- `src/litestar_gateway/infrastructure/llm/vertex_adapter.py`;
- `src/litestar_gateway/infrastructure/llm/gateway.py`;
- the process lifespan wiring in `src/litestar_gateway/app.py`.

If profiles confirm the lifecycle cost, introduce one infrastructure-level,
process-owned provider-client registry. Do not add an unbounded module global.

Required registry behavior:

- reuse async SDK clients and their HTTP connection pools across requests;
- use an explicit bounded capacity plus TTL/LRU eviction;
- isolate keys by provider, credential identity, credential material version or
  non-reversible in-process fingerprint, endpoint, API version, region/project
  and every constructor option that changes behavior;
- create exactly one client for concurrent misses of the same key;
- lease clients so eviction or credential rotation cannot close an in-use
  instance;
- replace rotated credentials immediately for new requests, then close the old
  client after active leases drain;
- close evicted and shutdown clients exactly once;
- keep synchronous and asynchronous lifecycles separate and never reuse a client
  across incompatible event loops;
- preserve provider timeout, retry and proxy behavior;
- close an individual response stream on disconnect while retaining its shared
  client;
- expose bounded, secret-free hit/miss/create/evict/lease metrics.

Assess OpenAI-compatible, Azure, Databricks, Anthropic and Vertex independently.
Assess Bedrock reuse only after verifying the boto client and executor
thread-safety contract.

Mandatory tests, written before implementation:

- sequential and concurrent calls for one key create one reusable client;
- distinct providers, credentials, material versions, endpoints, regions and
  Azure API versions cannot share;
- rotation swaps the client and closes the old one only after its last lease;
- eviction is bounded and closes each evicted client exactly once;
- shutdown closes all retained clients exactly once;
- cancellation closes the stream, not a shared client used concurrently;
- constructor failure does not poison the key or leak a partial client;
- provider errors, retries, native endpoints and Responses emulation preserve
  their contracts;
- reprs, logs, errors and exported metric labels contain no secret material;
- billing, budget, routing and usage regressions remain green.

Phase 1 exit criteria:

- before/after one-worker profiles prove whether client creation was material;
- the same benchmark is repeated with three workers;
- the PR reports throughput, p50/p95/p99, CPU/RPS, RSS and pool counts in both
  modes;
- no lifecycle, rotation, cancellation or tenant-isolation regression remains.

## Phase 2 — Optimize only the next measured hot spots

Re-profile after Phase 1 and rank costs. Investigate in measured order:

- repeated model, callable and credential database queries;
- credential envelope decryption and SDK credential parsing;
- Redis/team/key rate-limit and budget admission work;
- usage-event construction and transaction/commit latency;
- request/response Pydantic conversion and JSON/SSE serialization;
- response translation and Responses emulation;
- synchronous or verbose hot-path logging;
- DB-pool waits, connection count and event-loop lag.

For every slice, record a before profile, add a failing regression/performance
contract, make the smallest safe change, and rerun the same profile. Reject
changes whose gain is within run-to-run noise.

Any metadata cache must be bounded, tenant-safe and invalidated for credential
rotation, model disablement, revoked grants, API keys and policy changes. If
usage commits dominate, document current transaction/crash semantics before
proposing an outbox or batch. Validate persistence changes on SQLite and real
Postgres under concurrency and injected failure.

## Phase 3 — Tune the measured deployment

Keep runtime scaling distinct from code speedups.

Tasks:

- select the worker count from CPU quota explicitly; never assume one process
  will use multiple CPU-bound cores;
- benchmark one worker/one CPU to measure per-core progress and three
  workers/three CPUs to measure the deployment target;
- size SQLAlchemy pool and overflow per worker against Postgres connection
  capacity;
- size provider connection pools per worker and deployment, with bounded
  keep-alive and connection limits;
- evaluate supported event loop and HTTP parser choices only after profiling;
- test graceful shutdown, rolling replacement and stream cancellation with
  multiple workers;
- verify Redis-backed distributed limits and startup/migration safety before
  replicas are used;
- validate any estimate for 300 RPS with at least one real scale-up point.

Adding CPUs or workers is a deployment result, not a code optimization result.
Report both separately.

## Acceptance ladder

Use the 50 ms deterministic mock, one upstream attempt, 15-second minimum
ramp/settle and a 60-second minimum steady window. Run non-streaming and
streaming independently. Every stage reports successful completed RPS, all
failures, latency, CPU, RSS, event-loop lag and pool counts.

### Code-efficiency gate

Run one worker with a hard 1 CPU limit:

1. reproduce the current ~56 non-streaming RPS ceiling;
2. after Phase 1, demonstrate a statistically meaningful CPU/RPS improvement;
3. final goal: at least 100 non-streaming RPS/core at p95 <= 1 s;
4. report the streaming RPS/core ceiling separately.

### Three-worker deployment gate

Run three workers with a hard 3 CPU / 4 GiB limit:

1. Gate A: 100 completed RPS, zero gateway-generated 5xx, p95 <= 500 ms
   non-streaming and <= 750 ms streaming;
2. Gate B: 200 completed RPS, failure rate <= 0.1%, p95 <= 750 ms
   non-streaming and <= 1 s streaming;
3. Target: 300 completed RPS, failure rate <= 0.1%, p95 <= 1 s
   non-streaming, completion p95 <= 1.5 s streaming and TTFT p95 <= 250 ms;
4. sustained CPU <= 90% of the 3 CPU budget at the accepted target;
5. steady RSS <= 2 GiB for three workers, no monotonic growth over repeated
   stages and no leaked clients, streams, tasks or connections.

If a gate fails, do not describe the offered load as achieved throughput.
Record the maximum sustainable point, identify the measured bottleneck, and
provide a validated resource estimate for 300 RPS. Diagnostic runs may continue
to later stages, but acceptance remains failed.

## Verification

At minimum:

- focused unit and concurrent integration tests for registry lifecycle,
  isolation, rotation, eviction and shutdown;
- streaming disconnect/cancellation regressions;
- provider conformance and native endpoint tests;
- billing, budget, rate-limit, routing and usage suites;
- the full SQLite suite and real Postgres job;
- coverage of at least 80% for new code;
- `just lint`;
- `just typecheck`;
- `just pre-commit`;
- production Docker build and readiness smoke;
- deterministic 1-worker and 3-worker load profiles with local raw reports.

Never weaken an existing assertion or remove an exercised gateway invariant to
make a performance gate pass.

## Suggested PR sequence

1. **Benchmark contract:** permanent mock, independent modes, diagnostic/fail-fast
   controls, correct exit semantics and resource metrics.
2. **Provider client registry:** bounded lifecycle plus OpenAI-compatible/Azure
   adoption and complete concurrency/rotation tests.
3. **Remaining providers:** Anthropic and Vertex adoption, then Databricks and
   Bedrock only where their contracts permit safe reuse.
4. **Measured hot spot:** exactly one subsequent profile-proven bottleneck per
   reviewable PR.
5. **Production tuning:** worker, DB/provider pool and shutdown configuration
   with measured capacity documentation.

Every PR includes its baseline, after-result, raw-report location, test plan and
an explicit statement of preserved correctness/security invariants.

## Review blockers

- an unbounded or global SDK-client dictionary;
- a client key that contains a secret or omits a behavior-changing dimension;
- stale credentials usable after rotation;
- eviction or stream cleanup closing a client that still has active leases;
- shutdown leaking clients, tasks, streams or connections;
- multiplied per-worker pools exceeding deployment connection budgets;
- performance gains obtained by bypassing auth, policy, usage or billing work;
- benchmarks that use offered rather than completed throughput;
- live-provider-only conclusions;
- a 300 RPS claim without the stated CPU/memory constraints and raw evidence.

## Definition of done

Plan 14 is complete when the correctness and security gates pass and one of
these outcomes is measured:

1. the gateway sustains 300 completed non-streaming requests/s and 300 completed
   streams/s on 3 CPU / 4 GiB under the acceptance criteria; or
2. all profile-proven safe optimizations are delivered, the honest one-worker
   and three-worker ceilings are recorded, and a validated worker/CPU/replica
   plan demonstrates the resources required for 300 RPS.

The result must distinguish gateway capacity from provider capacity and code
efficiency from process scaling.
