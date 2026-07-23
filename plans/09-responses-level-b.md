# Plan 09 — Responses API Level B

**Design doc:** [`docs/next-steps/responses-level-b.md`](../docs/next-steps/responses-level-b.md)

**Depends on:** Plan 02 (complete) and the existing Responses emulation adapter.

**Status:** Phase 0, Phase 1a and Phase 1b-A/B (Anthropic + Bedrock) complete;
Vertex tool state is next.

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

## Phase 1b-A — Anthropic Chat tools — ✅ complete

- Maps OpenAI function definitions, `strict`, all four tool choices and
  `parallel_tool_calls` to the Anthropic Messages contract.
- Preserves provider `tool_use` IDs; groups parallel `tool_result` blocks in one
  user turn as required by Anthropic.
- Validates names, schema size/depth, JSON arguments and replay correlation
  before routing, budget admission and provider dispatch.
- Keeps client tools + structured output and streaming tools fail-closed.
- **Done:** the direct Chat two-turn loop passes through the stock OpenAI SDK;
  the emulated Responses loop passes an endpoint integration test. Malformed
  billable upstream tool output settles usage once and returns a sanitized 502.

## Phase 1b-B — Bedrock Converse tools — ✅ complete

- Map non-strict tools, assistant `toolUse`, user `toolResult`, `auto`, `any`
  and supported named choices.
- Add a model-family capability gate: Bedrock documents named choice only for
  Claude 3 and Nova.
- Keep `tool_choice=none` and `parallel_tool_calls=false` at 501 because Converse
  has no general equivalent.
- Keep `strict=true` and `json_schema` at 501 until the per-model Bedrock
  structured-output matrix and native `outputConfig.textFormat` mapping are
  explicit; do not simulate schema enforcement with a non-strict forced tool.
- Enforce Nova's documented top-level tool-schema subset.
- Reject unknown model families and opaque ARNs before routing; enable the
  proved Claude 3/Nova matrix only.
- **Done:** direct Chat and emulated Responses two-turn loops preserve IDs,
  arguments and ordered results; malformed billable upstream output settles
  usage once and returns a sanitized 502. Unsupported model/choice/streaming
  combinations fail before routing, budget admission or provider dispatch.

## Phase 1b-C — Vertex/Gemini tool state

- Implement direct Chat replay through Google's documented
  `tool_calls[].extra_content.google.thought_signature` carrier, preserving the
  opaque value exactly in both directions.
- Keep generic Responses tool loops fail-closed: normalized Responses
  function-call items still have no thought-signature carrier. Choose an
  explicit Responses solution: a wire-compatible stateless extension,
  tenant-bound gateway state, or a conservative model allowlist proven not to
  require signatures.
- Keep unsupported per-tool `strict` and disabled-parallel semantics fail-closed.
- **Done when:** direct Chat multi-step and parallel loops replay exact
  signatures through `extra_content`; Responses either gains an explicit safe
  carrier or remains 501, without using the degraded signature-validator bypass.

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
