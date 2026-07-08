# Plan 02 — Agent-framework compatibility

**Design doc:** [`docs/next-steps/agent-framework-compatibility.md`](../docs/next-steps/agent-framework-compatibility.md)
**Depends on:** Plan 01 (native endpoints) for the highest compatibility level;
the OpenAI-compatible surface + R7-H23 already cover the baseline.
**Theme:** make tool-calling agent frameworks work against the gateway, and prove
it with an automated conformance suite instead of prose promises.

## Two compatibility levels

1. **OpenAI-compatible surface** (`/v1/chat/completions`): works today for
   text/tools where the target provider is OpenAI/Azure/Databricks. After H23,
   providers that can't translate tools/vision **reject cleanly (501)** instead of
   silently degrading — so frameworks fail loudly, not wrongly.
2. **Native endpoints** (Plan 01): full-fidelity tool calling for Anthropic/Gemini
   via their own SDKs. This is where Anthropic/Gemini tool-calling agents belong.

The plan's job: make the routing between these two explicit, tested, and documented.

## Phase 1 — Conformance suite (the core deliverable)

A provider- and framework-parametrized test suite asserting each framework's
happy-path and tool-calling flow against the gateway.

- **Framework matrix** (start with the two highest-value, add the rest):
  - Pydantic AI (OpenAI-compatible provider pointing at the gateway).
  - OpenAI Python client / OpenAI Agents SDK style.
  - LangChain `ChatOpenAI`.
- **Per framework, assert:**
  - plain completion returns and is billed once;
  - a single tool call round-trips (request → tool_use → tool_result → final);
  - an unsupported combination (e.g. tools on a non-translating provider via the
    OpenAI surface) returns **501**, and the framework surfaces it as an error
    rather than hanging or fabricating.
- **Mechanism:** drive real framework clients against an in-process gateway with a
  faked upstream provider (reuse the existing `Fake*` client patterns in
  `tests/completions/`), so the suite is deterministic and needs no live API keys.
- **Done when:** the matrix is green in CI and a regression in request/response
  shape breaks a named framework test, not just a low-level unit test.

## Phase 2 — OpenAI-shaped requirements

Close the specific OpenAI-surface gaps agent frameworks depend on (from the design
doc's "OpenAI-shaped requirements"): tool/tool_choice echoing, `finish_reason`
values, streamed tool-call deltas, and error-shape parity. Each gap = one test in
the conformance suite first (RED), then the fix.

## Phase 3 — Documentation deliverables

Copy-paste setup snippets, one per framework, in the docs:

- Pydantic AI / OpenAI-compatible provider.
- LangChain `ChatOpenAI`.
- OpenAI client / OpenAI Agents SDK.
- A decision note: **when to use the OpenAI surface vs a native endpoint** (Plan 01)
  — native for Anthropic/Gemini tool-calling agents, OpenAI surface for
  provider-agnostic or OpenAI-target agents.

## File touchpoints

- New: `tests/conformance/` (parametrized framework × flow suite).
- Extended as gaps surface: `infrastructure/llm/openai_adapter.py`,
  `responses_emulation.py`, `feature_support.py`, `errors.py`.
- Docs: `docs/` framework guides + the surface-selection note.
- CI: the conformance suite runs in the existing test job (add the framework deps to
  the test extras).

## Risks & mitigations

- **Framework dep weight / flakiness** → fake the upstream provider; pin framework
  versions; keep the suite offline and deterministic.
- **Chasing every framework** → matrix is explicitly incremental; land Pydantic AI +
  OpenAI client first, expand only on demand.

## Execution

Phase 1 is one focused slice per framework (parallelizable across worktrees once the
`tests/conformance/` harness exists). Phases 2–3 follow from what phase 1 surfaces.
