# Plan 09 — Responses API Level B

**Design doc:** [`docs/next-steps/responses-level-b.md`](../docs/next-steps/responses-level-b.md)

**Depends on:** Plan 02 (complete) and the existing Responses emulation adapter.

**Status:** Phase 0 and Phase 1a complete; Phase 1b is next.

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

## Phase 1a — Non-streaming tools over OpenAI-compatible chat — ✅ complete

- Translate function tool definitions, selection and parallel-call intent to the
  chat request.
- Translate replayed `function_call` items and matching
  `function_call_output` input to assistant/tool messages, enabling stateless
  loops while provider-owned response state remains disabled.
- Convert chat `tool_calls` to Responses `function_call` output items with stable
  IDs and arguments.
- Keep streaming tools fail-closed until Phase 2.
- **Done:** a complete two-turn loop succeeds through `/v1/responses` against a
  fake Databricks/OpenAI-compatible chat provider. Each upstream invocation is
  billed exactly once; parallel calls preserve order, IDs and argument strings.

## Phase 1b — Provider-native chat tool adapters

- Add faithful non-streaming Chat tool contracts to the Anthropic, Vertex and
  Bedrock adapters before enabling their Responses capability.
- Preserve provider call IDs across tool-use/tool-result representations.
- Keep each provider at a pre-admission 501 until its direct Chat tool loop is
  conformance-tested; do not let the generic Responses translator outrun the
  wrapped adapter.
- **Done when:** the same Phase 1a pure and endpoint contract passes for all
  emulated providers without weakening structured-output behavior.

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
