# Plan 15 — Post-throughput next steps: benchmark hardening, the 300-RPS decision, and the feature sequence

This plan is the execution bridge from the completed Plan 14 (gateway
hot-path throughput, PRs #348–#353) to the feature roadmap in
[README.md](README.md). It owns three things:

1. **Phase A** — make the benchmark contract immune to the measurement
   artifact discovered on 26 July 2026 (Docker Desktop's host→container port
   proxy invalidates sustained streaming numbers on macOS), then finish the
   measurements Plan 14 left open;
2. **Phase B** — a single documented decision: keep optimizing for
   300 RPS on 3 CPU, or accept a larger footprint and stop;
3. **Phase C** — the order in which the existing feature plans (09, 05, 04,
   07, 10) get picked up, with entry criteria, referencing their own design
   docs rather than duplicating them.

Ground rules inherited from Plan 14 (they keep applying verbatim):

- every performance claim = 3 runs, medians retained; gains ≤3% are noise;
- never change benchmark parameters and production code in the same PR;
- one reviewable PR per slice, full gate before each
  (`just lint`, `just typecheck`, `just pre-commit`, full suite);
- streaming numbers at ≥60 s measured through the macOS host proxy are
  **not valid evidence** (Plan 14a, Step 6 resolution) — until Phase A1
  lands, any such run must be executed in-network manually.

## Phase A — Benchmark contract hardening and open measurements

### A1. Containerize the load generator (PR, ~half day)

Goal: `just load-contract` runs Locust **inside the Docker network**
(container→container, `http://app:8000`), eliminating the host-proxy path
that produced false streaming failures at t≈60 s.

Design (decided here so the PR is mechanical):

- **New Dockerfile stage `loadgen`**, built `FROM builder` (which already
  contains uv, the repo checkout and the main venv):
  `RUN uv sync --frozen --no-dev --group load`. Never part of `runtime`.
- **New compose service `loadgen`** in `docker-compose.benchmark.yml`:
  `build.target: loadgen`, `profiles: ["loadgen"]` so `up` does not start
  it; attached to the default network; bind-mounts the host run directory
  at `/out` (writable) for CSV/HTML reports. No ports.
- **Stage execution** in `scripts/run_load_profile.py`: when
  `LOAD_IN_NETWORK=1` (exported by `scripts/benchmark-compose.sh run`),
  `build_locust_command()` emits
  `docker compose --project-name … --file … run --rm --quiet-pull
  loadgen locust -f scripts/locustfile.py --headless --host http://app:8000
  --csv /out/<mode>-<label> --html /out/<mode>-<label>.html`
  with the stage's `LOAD_*` variables passed via `-e` flags (never the
  bootstrap secrets — same `BOOTSTRAP_ONLY_ENVIRONMENT` stripping as today).
  `docker compose run` propagates the container's exit code, so the
  threshold-aligned exit semantics from Plan 14 Phase 0 are preserved
  unchanged.
- **Host-target validation**: `validate_load_host()` currently requires
  HTTPS except explicit loopback. Extend with one narrowly-scoped
  exception: plain `http://app:8000` is accepted **only when**
  `LOAD_IN_NETWORK=1`. The default (host-side) behavior must not weaken.
- **Bootstrap stays on the host** (`run_benchmark_contract.py` via the
  published `127.0.0.1:18000` port): it is low-rate management traffic,
  unaffected by the artifact, and keeps admin credentials off the loadgen
  container's environment.
- `DockerStatsSampler` and run metadata: unchanged (host-side observers).

Tests (extend the existing `tests/load/` suites, same style):

- `build_locust_command()` with `LOAD_IN_NETWORK=1` produces the
  `docker compose run` form, in-network host, `/out` prefixes, and stage
  env `-e` flags with no bootstrap secret present;
- `validate_load_host()` accepts `http://app:8000` only with the flag set,
  and still rejects every non-loopback plain-HTTP host without it;
- compose-contract test (`tests/load/test_benchmark_compose.py` pattern):
  the `loadgen` service exists, is profile-gated, has no ports, and mounts
  `/out`.

Exit criteria:

- `LOAD_STAGES="200" LOAD_MODES=chat-stream LOAD_DURATION_SECONDS=90 just
  load-contract` reproduces the known-good in-network result
  (~199.5 RPS, 0% failures, p95 ≈ 270 ms) with zero manual steps;
- the six-stage default profile still runs end-to-end with correct exit
  codes in both fail-fast and diagnostic policies;
- docs: `docs/operations.md` benchmark section updated; Plan 14a Step 6
  "Consequence for the benchmark contract" paragraph annotated as resolved
  by this PR.

### A2. Probe the true in-network streaming ceiling (evidence only, ~2 h)

Blocked by A1 (or run manually in-network if A1 is deferred).

Protocol: `LOAD_STAGES="200,250,300" LOAD_MODES=chat-stream
LOAD_DURATION_SECONDS=60 LOAD_PROFILE_POLICY=diagnostic just load-contract`,
3 runs, medians; 3 workers / 3 CPU / 4 GiB, 50 ms mock — identical
conditions to the frozen v1 baseline except the (now valid) network path.

Exit criteria:

- Plan 14/14a tables updated: the streaming column gains real numbers at
  250 and 300 offered (today the honest record stops at "≥199.5 at 200");
- the non-streaming rows are re-run in-network once (3 runs) to confirm the
  ~247 RPS ceiling was not proxy-affected too — if it moves >3%, all Plan 14
  after-numbers get a footnote and the in-network figures become canonical.

### Deferred nice-to-have: a granian server swap (after Phase C)

Deliberately parked, not planned — pick it up only once the Phase C
features are done. Context recorded for whoever picks it up: verified
26 July 2026, the production image **already runs uvloop + httptools**
(pulled transitively by `mlflow-skinny → uvicorn[standard]`), so uvicorn is
already on the fast event loop and the realistic granian upside sits in the
5–15% band. The spike is ~1 day with four hard gates, in order (failing any
one rejects it): security parity for
`--proxy-headers`/`--forwarded-allow-ips` (the per-IP rate limit and audit
log depend on the real client IP), SSE-disconnect parity (lease released,
no shared client force-closed), lifespan/shutdown parity
(`LLMGatewayImpl.aclose()` runs last on SIGTERM), and the standard 3-run
measurement with the >3% adoption threshold. Rollback is a one-line
entrypoint revert.

## Phase B — The 300-RPS decision (decision, not code)

Input: the A2 ceilings. Current honest position: non-streaming ~247 RPS,
streaming ≥199.5 RPS on 3 CPU; the remaining CPU profile is diffuse
(no frame >1%, Plan 14a Step 5).

Decide between:

- **B-i. Stop optimizing.** Declare the 3-CPU ceilings final for this
  round; the 300/300 target is met by deployment (4–5 CPU or a second
  replica — Plan 14's own arithmetic). Close Plan 14 as outcome 2, final.
- **B-ii. One more optimization slice.** The Phase-2 metadata/credential
  cache (bounded, tenant-safe, invalidated on rotation/revoke/disable/policy
  change — Plan 14's constraints apply in full), estimated 2–3 days for an
  estimated +10–20%. Only justified if the product genuinely requires
  300 RPS on exactly 3 CPU.

Default recommendation: **B-i**, because Phase C features are worth more
than the last ~20% of per-core headroom, and the cache slice stays available
later with its design constraints already written down.

Exit criterion: the decision and its rationale are recorded in Plan 14a
(one paragraph, dated); if B-ii, it becomes its own plan-14a Step 7 with the
full TDD protocol.

## Phase C — Feature sequence

Each item references its existing design doc; "started" means: branch
created, first failing test written. Entry criteria are cumulative (an item
starts only when the previous is merged or consciously parked).

| # | Feature | Plan doc | Why this position |
|---|---------|----------|-------------------|
| C1 | Responses API streaming tool events | [09-responses-level-b.md](09-responses-level-b.md) (phase 2) | The only *half-finished* surface (streaming tools are fail-closed today). Finish before opening new fronts; the repo roadmap itself puts it first. |
| C2 | Cross-provider failover | [05-cross-provider-failover.md](05-cross-provider-failover.md) | The category-defining reliability feature: fall over on 429/5xx/timeout to a capable candidate. Sequence per its own doc: sequential failover → observability → circuit breaker. |
| C3 | Response caching | [04-response-caching.md](04-response-caching.md) | Direct provider-cost savings (exact-match first). Its tenant-isolation threat model is the review centerpiece. |
| C4 | Budget alerts | [07-budget-alerts.md](07-budget-alerts.md) | Small, off the hot path, high governance value (today budgets block at 100% with no warning at 80%). |
| C5 | Usage analytics | [10-usage-analytics.md](10-usage-analytics.md) | Temporal cost/token/call series per team, built on the already-authoritative billing data. |

Parallel track (no fixed slot, pick up when capacity allows): Plan 11
(OpenAPI/migration drift gates, request correlation) and Plan 13 (decimal
money, image/cache-token pricing) — the repo roadmap's "trust the pipeline"
and "money correctness" items. They gate *scale-out*, not the next feature,
so they queue behind C1–C3 rather than blocking them.

Explicitly rejected for now (recorded so it doesn't resurface as an open
question): **rewriting the call path in Rust**. The measured bottleneck was
never interpreter speed — it was a per-request construction bug (fixed in
Python, +21–49%), then a measurement artifact (fixed by moving the load
generator). The remaining CPU profile is diffuse; the same business outcome
(300 RPS) costs 1–2 extra cores versus a multi-month rewrite of the entire
auth/billing/translation hexagon into a second language. Reconsider only if
the product pivots to ultra-dense proxying (≥10k RPS/node) where per-core
efficiency is itself the product.

## PR sequence

| PR | Content | Gate |
|----|---------|------|
| 15-1 | A1: loadgen stage + compose service + in-network stage execution | tests/load suites green; in-network stream-200 reproduces ~199.5 RPS/0% |
| 15-2 | A2: docs-only ceiling update (14/14a tables) | 3-run medians attached |
| 15-3 | B: decision paragraph in Plan 14a | none (docs) |
| C-* | one PR chain per feature, per its own plan doc | that plan's own gates |

## Risks

- A1's `docker compose run` adds ~1–2 s startup per stage; acceptable for
  acceptance runs, annoying for quick iteration — keep the host-path
  execution working (default when `LOAD_IN_NETWORK` is unset) for
  short-window chat-only smoke runs, with the documented caveat.
- The loadgen image build lengthens `load-contract` cold starts; it shares
  the builder stage cache, so warm rebuilds stay cheap.
- A2 may show the in-network non-streaming ceiling differs from ~247 —
  that is a *correction*, not a regression; Plan 14's tables get footnoted,
  not rewritten.
