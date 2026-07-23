# Design doc — Routing evolution

> **Status:** proposed. Smart routing, immutable revisions, shadow decisions and
> distillation export are shipped. This design adds capability discovery,
> evidence-based promotion, dry-run simulation and same-protocol routing on native
> endpoints. Execution plan:
> [`plans/12-routing-evolution.md`](../../plans/12-routing-evolution.md).

## 1. Capability discovery

First define an authoritative per-model capability block: operations, protocol
surfaces, tools, vision, JSON schema, context window and streaming support.
Today the gateway registry owns provider-level operations, while feature flags
are manual profiles repeated on router candidates. The new schema must define
how router capabilities aggregate before `/v1/models` publishes them; discovery
then derives from that schema rather than adding another hand-maintained matrix.

Clients can then choose `/v1/chat/completions`, `/v1/responses`,
`/v1/messages` or Gemini native endpoints without trial-and-error 501s.
Existing `/v1/models` fields remain backward compatible.

## 2. Shadow comparison and promotion

Persist a server-generated comparison ID on both active and shadow decisions,
alongside the request correlation ID from Plan 11. Aggregate paired decisions by
immutable router revision: agreement rate, model distribution, latency and
estimated cost. If offline labels exist, include accuracy; never pretend
agreement alone is quality.

Promotion is always an explicit, audited platform-admin action with optimistic
revision checks. The system may recommend a revision after configured evidence
thresholds, but must not auto-promote it.

## 3. Cost/policy simulator

Before activation, run a router revision against a bounded, explicitly supplied
evaluation corpus containing the inputs required by its strategies and report:

- requests rejected by capability filters;
- predicted candidate distribution;
- estimated cost and savings;
- cases near a hybrid boundary or requiring external strategies.

Historical metadata can supply only cost/distribution baselines reconstructible
without prompt text; it cannot replay complexity, embeddings, judge or hybrid
routing by itself. External strategies are disabled or explicitly mocked in
dry-run mode. Simulation does not call providers, mutate decisions or retain
the evaluation corpus after the run.

## 4. Same-protocol native routing

Allow a native Anthropic request to route only among Anthropic candidates and a
native Gemini request only among Gemini candidates. Reject a router containing a
different protocol family at config time and revalidate at dispatch.

The chosen request/response remains native and untranslated. Capability filters
use native semantics; no OpenAI↔Anthropic/Gemini tool mapping is introduced.
Metering and attribution remain those of the native endpoint.

## 5. Boundaries

- Cross-provider failover is Plan 05, not native routing.
- Automatic adaptive bandits remain out of scope.
- Router recommendations never bypass revision pinning, RBAC or audit.
