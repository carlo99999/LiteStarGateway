# Plan 02 — Framework-agnostic wire-contract conformance

**Design doc:** [`docs/next-steps/agent-framework-compatibility.md`](../docs/next-steps/agent-framework-compatibility.md)
**Depends on:** nothing for the OpenAI-surface contract (available today); the
native Anthropic/Gemini contracts depend on Plan 01.
**Theme:** guarantee the gateway works with **any** agent framework — by
conforming to the standard **wire protocols**, validated by contract, not by
chasing individual frameworks.

## Principle: agnostic by contract, not by framework

A framework is compatible iff it speaks a wire protocol the gateway implements
faithfully. So the gateway needs **zero per-framework code**: it implements the
OpenAI Chat Completions protocol today, and the native Anthropic Messages / Gemini
`generateContent` protocols after Plan 01. Get the contracts right and every
compliant client — Pydantic AI, LangChain, the OpenAI Agents SDK, or anything
built next year — works for free.

- ❌ **Framework-coupled** (rejected): "test that Pydantic AI works, test that
  LangChain works" — an endless matrix that couples us to each framework's quirks.
- ✅ **Contract-based** (this plan): assert the gateway's request/response/streaming/
  error shapes match the protocol spec. Use the **official provider SDKs** as
  end-to-end canaries (proof the contract holds in practice), never as the
  definition of done. If a specific framework breaks, fix the **contract** (only
  when the defect is legitimate against the spec) — never add a per-framework branch.

## Two contract surfaces

1. **OpenAI Chat Completions** (`/v1/chat/completions`) — available now. After
   R7-H23, providers that cannot translate tools/vision **reject cleanly (501)**
   instead of silently degrading, so clients fail loudly, not wrongly.
2. **Native Anthropic Messages / Gemini `generateContent`** — after Plan 01. This
   is the agnostic path for the Anthropic/Gemini ecosystems: any client speaking
   those native protocols (their own SDKs, or frameworks built on them) works.

## Phase 1 — OpenAI Chat Completions contract conformance (existing surface)

The core deliverable, buildable now against what already ships.

- **Contract assertions** (the shapes any compliant client depends on):
  - response envelope: `choices`, `message`, `finish_reason` values, `usage`;
  - tool calling: `tool_calls` request echo + response shape, `tool_choice`,
    `finish_reason == "tool_calls"`, and a tool-result round-trip;
  - streaming: chunk/delta shape, incremental tool-call deltas, terminal chunk;
  - errors: OpenAI-shaped error envelope + correct HTTP status (incl. the H23
    501 for untranslatable tools/vision on non-OpenAI providers).
- **Canary, not matrix:** drive the assertions with the **official `openai`
  Python SDK** pointed at an in-process gateway with a **faked upstream provider**
  (reuse the `Fake*` client patterns in `tests/completions/`) — deterministic, no
  live keys. One real client proves the wire works; the contract tests define done.
- **Done when:** `tests/conformance/` is green in CI and a change to the
  request/response/stream/error shape breaks a **contract** test (not just a
  low-level unit test), for OpenAI/Azure/Databricks-backed models.

## Phase 2 — Close OpenAI-shape contract gaps

Whatever Phase 1 surfaces (from the design doc's "OpenAI-shaped requirements":
tool/tool_choice echo, `finish_reason` values, streamed tool-call deltas, error-
shape parity). Each gap = a failing contract test first (RED), then the fix in the
adapter/emulation layer — never a per-framework special case.

## Phase 3 — Documentation (protocol-based)

- "Point any OpenAI-compatible client at the gateway" — one snippet with the stock
  `openai` SDK; note that any framework layering on it (Pydantic AI, LangChain,
  OpenAI Agents SDK) inherits this for free.
- The surface-selection note: OpenAI surface for OpenAI-target/provider-agnostic
  clients; **native endpoints (Plan 01)** for Anthropic/Gemini tool-calling.

## After Plan 01 — extend conformance to the native contracts

Add native-protocol contract suites (Anthropic Messages, Gemini `generateContent`)
using the official `anthropic` / `google-genai` SDKs as canaries. Same principle:
assert the native wire contract, no per-framework code. This is also Plan 01's
acceptance layer — build the native contract tests as the RED that Plan 01 turns
GREEN.

## File touchpoints

- New: `tests/conformance/` (contract-assertion suites; SDK-canary fixtures).
- Extended as gaps surface: `infrastructure/llm/openai_adapter.py`,
  `responses_emulation.py`, `feature_support.py`, `errors.py`; later the native
  adapters.
- Docs: protocol-based client guides + the surface-selection note.
- CI: the conformance suite runs in the existing test job (add SDK test extras).

## Risks & mitigations

- **A framework depends on an undocumented quirk** → fix the contract only if the
  quirk is legitimate against the spec; otherwise it is the framework's bug, not
  ours. Never branch on framework identity.
- **SDK dep weight / flakiness** → fake the upstream provider; pin SDK versions;
  keep the suite offline and deterministic.

## Execution

Phase 1 is one focused slice (contract suite + `openai` SDK canary). Phases 2–3
follow from what it surfaces. The native-contract extension runs after Plan 01,
reusing the same harness.
