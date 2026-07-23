# Plan 09 — Responses API Level B

**Design doc:** [`docs/next-steps/responses-level-b.md`](../docs/next-steps/responses-level-b.md)

**Depends on:** Plan 02 (complete) and the existing Responses emulation adapter.

**Theme:** eliminate silent feature drops, then add faithful tool-call items and
events for chat-only upstreams.

## Phase 0 — Fail loudly

- Add a pure emulation-capability validator.
- Add a provider-aware preparation hook after model resolution and before
  `UsageMeter.admit`; use it for emulation capability checks.
- Reject `tools` only until Phase 1 lands; always reject still-lossy multimodal,
  stateful, built-in-tool and reasoning inputs.
- Audit the native Responses allowlist and retain every field the native
  adapters support.
- Map the rejection to the existing OpenAI-shaped 501 response.
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
- Regression: native Responses passthrough stays byte/shape compatible.
- Regression: unsupported features fail before provider invocation and before
  budget admission.
- Run the full Python gate and conformance suite after every phase.
