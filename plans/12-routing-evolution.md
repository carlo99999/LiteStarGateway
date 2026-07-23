# Plan 12 — Routing evolution

**Design doc:** [`docs/next-steps/routing-evolution.md`](../docs/next-steps/routing-evolution.md)

**Depends on:** shipped router revisions/shadow mode. Plan 11-A request
correlation is required before paired active/shadow comparison. Temporal
analytics from Plan 10 improves cost baselines but is not required for
discovery.

**Theme:** make routing discoverable, measurable and safely promotable.

## Phase 1 — Capability discovery

- Define the authoritative per-model capability schema first. Today operation
  support is provider-level while tools/vision/JSON/context are manual
  per-router-candidate profiles; document aggregation semantics for routers.
- Derive discovery from that schema and the gateway operation registry.
- Extend `/v1/models` compatibly and document the schema.
- Contract tests ensure advertised capabilities match real 200/501 behavior.

## Phase 2 — Shadow comparison

- Persist one server-generated comparison ID on the active and shadow decisions,
  alongside the Plan 11 request correlation ID.
- Revision-scoped paired aggregates: agreement, distribution, latency, estimated
  cost and optional labelled accuracy.
- Console comparison view with explicit insufficient-data states.

## Phase 3 — Promotion workflow

- Recommendation thresholds over Phase 2 evidence.
- Explicit platform-admin promote action with CAS, audit and rollback to a prior
  immutable revision.
- No autonomous promotion.

## Phase 4 — Dry-run simulator

- User-supplied, bounded evaluation corpus with the inputs required by each
  selected strategy; historical metadata is limited to cost/distribution
  baselines that can be reconstructed without prompt text.
- No provider calls; external strategies disabled/mocked.
- Predicted distribution/cost/reject report in API and console.

## Phase 5 — Native-family routing

- Validate every router candidate belongs to the endpoint's protocol family.
- Reuse strategy/capability selection, then dispatch through the native adapter
  with native metering.
- Conformance tests with official Anthropic/Gemini SDK canaries.

## Verification

- TDD with advertised-vs-actual matrix tests.
- Security tests for cross-family rejection, tenant scope, revision CAS and RBAC.
- Audit every promotion; never persist additional prompt bodies for simulation.
