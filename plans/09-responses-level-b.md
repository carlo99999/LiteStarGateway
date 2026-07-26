# Plan 09 — Responses API Level B

**Design doc:** [`docs/next-steps/responses-level-b.md`](../docs/next-steps/responses-level-b.md)

**Depends on:** Plan 02 (complete) and the existing Responses emulation adapter.

**Status:** Phase 0, Phase 1a and Phase 1b-A/B (Anthropic + Bedrock) complete;
Direct Vertex Chat tool state is complete; Phase 2 streaming tool events are
done for Databricks and Anthropic — Bedrock streaming translation (accepted
known limitation, not planned) and generic Vertex Responses state remain.

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

- **Done:** direct Chat replay for validated Gemini 2.5/3 text models preserves
  the opaque thought_signature byte-exactly in both directions. Parallel and
  sequential replay preserve call/result ordering; Gemini 3 requires the first
  call signature.
- **Done:** unsupported per-tool `strict`, disabled-parallel semantics,
  unvalidated model families and streaming tools fail before routing/admission.

**Decision (26 July 2026): adopt LiteLLM's carrier scheme.** The original
`tool_calls[].extra_content.google.thought_signature` side-channel field
(shipped above) could never reach the Responses surface: the generic
Responses<->Chat translators in `responses_emulation.py` copy a tool call's
`id`/`name`/`arguments` only, dropping any other key, so `extra_content`
never survived that translation. Researched how LiteLLM solves the same
problem (GitHub PRs #18374, #16895) — they encode the signature directly
into the tool call's own id (`call_123__thought__<signature>`) rather than a
side field, since both OpenAI Chat and Responses already require the client
to echo a call's id back verbatim to correlate its result. That id-level
carrier survives generic translation for free, because copying the id is
the one thing both surfaces' translators already do.

Adopted as `encode_vertex_call_id`/`decode_vertex_call_id` in
`chat_tool_policy.py` (delimiter `__thought__`), replacing the extra_content
field entirely — a **behavior-preserving migration** on the direct Chat
surface (same byte-exact replay, same first-call-only and Gemini-3-required
constraints, same synthetic-call-id marking for Gemini-omitted ids, all
re-verified by the existing test suite updated to the new encoding). This is
the prerequisite, not the unlock itself: with the carrier now living in the
id, a follow-up PR only needs to remove Vertex's Responses tool guard (in
`request_policy.py`) and add the same model-family/parallel/signature
validation that already exists for the Chat surface — no changes needed to
the Responses<->Chat translators themselves, since copying the id is already
their job.

**Done (26 July 2026): non-streaming Vertex tool calls now work on
`/v1/responses`.** `Provider.VERTEX_AI` added to
`_EMULATED_RESPONSES_TOOL_PROVIDERS`/`_BOUNDED_TRANSLATED_TOOL_PROVIDERS`;
model-family (`vertex_supports_tools`) and disabled-parallel rejection
mirror the Bedrock block exactly. `strict` is rejected outright (any value,
matching the Chat surface — not just non-bool values), closing an
early-rejection gap that only Bedrock had before. A full two-turn tool loop
through `/v1/responses` (declare tool → call → result → final answer)
verifies byte-exact signature replay, correct real-vs-synthetic id
handling, and billing, exactly matching the direct Chat surface's existing
test. Streaming Vertex tool calls remain fail-closed — a separate gap
(Converse/Gemini stream event translation), not this decision.

**Known, accepted gap:** `request_policy.py`'s pre-dispatch tool validation
does not exhaustively mirror every Chat-surface constraint early (e.g. deep
schema/JSON-depth checks) for every case; anything it misses is still
caught by `to_gemini_request`'s call into `validate_chat_request` before
any provider network call — just after budget admission rather than
before. Not a correctness gap, only a "how early" one; extend
`request_policy.py`'s block only if a real cost-avoidance need arises.

## Phase 2 — Streaming tool events

- Preserve call index/ID across fragmented chat deltas.
- Emit the ordered `output_item` and `function_call_arguments` event sequence.
- Support multiple parallel calls without cross-contaminating arguments.
- **Done when:** the stock OpenAI SDK accumulates arguments correctly and an
  incomplete/malformed upstream sequence never produces `response.completed`.

**Databricks/OpenAI-compatible slice — ✅ complete** (`ChatToResponsesAdapter
.astream_responses`, `responses_emulation.py`): accumulates
`choices[0].delta.tool_calls[].function.arguments` by stream index, emitting
`response.output_item.added` → N × `response.function_call_arguments.delta`
→ `response.function_call_arguments.done` → `response.output_item.done` per
call, in first-seen order; a stream that ends without `finish_reason ==
"tool_calls"` while any call is still open raises instead of completing.

**Anthropic slice — ✅ complete** (`anthropic_event_to_delta`,
`anthropic_adapter.py`): `content_block_start` with `content_block.type ==
"tool_use"` now emits an initial `tool_calls` delta (index/id/name);
`input_json_delta` maps to `tool_calls[].function.arguments` for tracked
indexes instead of the old blanket `content` mapping. The forced
structured-output tool (Anthropic has no native JSON mode) is excluded by
name from this tracking, so its `input_json_delta` events keep relaying as
`content` exactly as before — unaffected by this change. `stop_reason ==
"tool_use"` now maps to OpenAI `finish_reason: "tool_calls"` (previously
`"stop"`, which the non-streaming path already special-cased but the
streaming path didn't) whenever any real tool call was tracked. Both
`validate_responses_request`'s and `validate_chat_request`'s streaming-tool
guards now exclude Anthropic — the latter required separating the existing
"enforced non-streaming bypassed by a raw `stream: true` request" check
(must always fail, independent of provider support) from the
provider-support check itself (`_STREAMING_TOOL_TRANSLATED_PROVIDERS`).

**Bedrock — accepted known limitation, not planned.** `converse_event_to_delta`
currently raises `UpstreamResponseInvalid` for *any* non-text content block
or delta (`contentBlockStart`/`contentBlockDelta` with `toolUse`), so
Bedrock streaming tool calls fail loud today rather than producing wrong
output — an acceptable state to leave as-is. `_STREAMING_RESPONSES_TOOL_PROVIDERS`
and `_STREAMING_TOOL_TRANSLATED_PROVIDERS` both still exclude Bedrock.

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
