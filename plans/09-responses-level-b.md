# Plan 09 — Responses API Level B

**Design doc:** [`docs/next-steps/responses-level-b.md`](../docs/next-steps/responses-level-b.md)

**Depends on:** Plan 02 (complete) and the existing Responses emulation adapter.

**Status:** Phase 0 complete; Phase 1 is next.

**Theme:** eliminate silent feature drops, then add faithful tool-call items and
events for chat-only upstreams.

## Phase 0 — Fail loudly — ✅ complete

- Added a pure emulation-capability validator.
- Added a provider-aware preparation hook after model resolution and before
  `UsageMeter.admit`; use it for emulation capability checks.
- Rejects `tools` only until Phase 1 lands; always rejects still-lossy multimodal,
  stateful, built-in-tool and reasoning inputs.
- Audited the native Responses allowlist and retained every synchronous,
  stateless field the adapters can bill and isolate safely.
- Forces native `store=false`; hosted tools, extended cache retention and
  provider-owned resource IDs remain fail-closed until their cost and ownership
  are governed.
- Maps rejection to the existing OpenAI-shaped 501 response, including before
  an SSE stream opens.
- **Done when:** every Responses field accepted by the sanitizer is either
  translated or rejected before the fake provider is invoked.

## Phase 1 — Non-streaming tools

- Translate function tool definitions, selection and parallel-call intent to the
  chat request.
- Translate `function_call_output` input to matching tool-result messages.
- Convert chat `tool_calls` to Responses `function_call` output items with stable
  IDs and arguments.
- **Done when:** a complete tool loop succeeds through `/v1/responses` against a
  chat-only fake provider and bills exactly once.

## Phase 2 — Streaming tool events

- Preserve call index/ID across fragmented chat deltas.
- Emit the ordered `output_item` and `function_call_arguments` event sequence.
- Support multiple parallel calls without cross-contaminating arguments.
- **Done when:** the stock OpenAI SDK accumulates arguments correctly and an
  incomplete/malformed upstream sequence never produces `response.completed`.

## Phase 3 — SDK canaries and documentation

- Add an OpenAI Agents SDK canary while keeping assertions at the wire boundary.
- Update `docs/agent-frameworks.md` and the Level A/B/C matrix.
- Document unsupported stateful/reasoning/multimodal features explicitly.
- **Done when:** both SDK canaries run offline against the in-process app and CI
  locks the Level B subset.

## TDD and risk gates

- Write translator/event tests first; then endpoint integration; then SDK canary.
- Regression: governed native Responses passthrough stays byte/shape compatible;
  background, tier selection and opaque stored-state IDs remain fail-closed.
- Regression: unsupported features fail before provider invocation and before
  budget admission.
- Run the full Python gate and conformance suite after every phase.
