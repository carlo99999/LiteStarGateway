# Design doc — Responses API Level B

> **Status:** Phase 0, Phase 1a and Phase 1b-A/B shipped. `/v1/responses` preserves the governed,
> synchronous and stateless OpenAI SDK request surface for OpenAI/Azure and
> supports text over chat-only providers and structured-output emulation where
> the selected provider contract can enforce it.
> Databricks, Anthropic and capability-gated Bedrock Claude 3/Nova models
> additionally support faithful non-streaming function-tool loops over their
> translated Chat surfaces. Validated Vertex Gemini 2.5/3 text models now
> support the direct Chat loop with byte-exact thought-signature replay.
> Unsupported fields fail with 501 before router side effects, budget admission
> or provider dispatch. Generic Vertex Responses tool state and streaming tool
> events remain.
> Execution plan:
> [`plans/09-responses-level-b.md`](../../plans/09-responses-level-b.md).

## 1. Problem

`domain/request_policy.py` accepts the native Responses SDK surface, while
`infrastructure/llm/responses_emulation.py` maps only the faithful chat subset:
text, instructions, sampling controls and structured output. Phase 0 added a
provider-aware gate so unsupported fields are rejected rather than ignored.

That is incompatible with the gateway's fail-loud contract: an accepted feature
must be represented faithfully or rejected clearly before the provider call.
Silent degradation is never an acceptable compatibility mode.

## 2. Goal

For chat-only upstreams:

- translate function tools and tool-result input without changing tool names,
  JSON arguments or stable call IDs;
- emit the Responses output-item and streaming event sequence expected by the
  stock OpenAI SDK and the OpenAI Agents SDK;
- meter and budget the call through the existing `CompletionService` pipeline;
- reject every still-lossy feature with `UnsupportedOperation` before dispatch.

Native Responses providers remain passthrough and must not regress.

Phase 1a enabled this subset for Databricks. Phase 1b-A added Anthropic with
native choice/strict/parallel mappings. Phase 1b-B added non-strict Bedrock
`toolSpec`/`toolUse`/`toolResult` mappings for validated Claude 3/Nova model IDs.
Both preserve exact call IDs and grouped parallel results. Vertex direct Chat
now does the same while preserving Google's signature carrier; generic Vertex
Responses tools remain fail-closed because normalized function-call items
cannot carry that state.

## 3. Fail-loud capability gate

The provider-aware Responses capability validator runs after model resolution
and output clamping but before `UsageMeter.admit`; emulated paths therefore
validate before `to_chat_completions()`. It owns the supported subset and
returns a precise 501 for unsupported multimodal input, stateful conversations,
built-in tools or reasoning items.

Error text must name the unsupported field and, where useful, point to a native
endpoint. No unsupported request may reserve budget or reach an adapter after a
field has been silently discarded. Native providers also get an allowlist
parity test against the installed SDK so supported passthrough fields are not
stripped prematurely. A separate governance gate rejects background execution,
client-selected service tiers, hosted tools with non-token fees, extended cache
retention and opaque stored-state references until asynchronous settlement,
tier-aware pricing and tenant-bound provider resources exist. Native calls
force `store=false` when the client omits it.

## 4. Tool translation

The supported request mapping includes:

- Responses function-tool definitions → Chat Completions `tools`;
- `tool_choice` and `parallel_tool_calls` unchanged where the provider supports
  them;
- Responses message input → chat messages;
- replayed Responses `function_call` items → one or more assistant
  `tool_calls` (consecutive calls stay grouped);
- `function_call_output` input → a `role="tool"` message with the matching
  `tool_call_id`.

For Anthropic, those Chat messages become native `tool_use` blocks and
consecutive results become one user turn containing all matching `tool_result`
blocks. `required` maps to `any`; a named choice maps to `tool`; disabled
parallelism maps to `disable_parallel_tool_use=true` as documented in
[Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use).

For Bedrock, definitions become `toolSpec` entries, assistant calls become
`toolUse` blocks and consecutive results become one user turn with ordered
`toolResult` blocks. `required` maps to `any`; named choice maps to `tool` for
validated Claude 3/Nova IDs. `none` and disabled parallelism remain precise 501s
because Converse has no general equivalents. `strict=true` and `json_schema`
also remain fail-closed until Bedrock's per-model structured-output capability
matrix and native `outputConfig.textFormat` mapping are implemented. Nova tool
schemas use its documented top-level `type`/`properties`/`required` subset.

The reverse mapping turns chat `message.tool_calls` into Responses
`function_call` output items. Preserve provider-issued IDs when present; generate
stable response item IDs only when the chat protocol does not supply one.
This stateless replay follows the official
[function-calling flow](https://developers.openai.com/api/docs/guides/function-calling#function-tool-example):
the caller appends response output items, then the correlated tool output, to
the next request.

## 5. Streaming event contract

For a tool call, emit a valid ordered sequence including:

1. `response.created`;
2. `response.output_item.added`;
3. zero or more `response.function_call_arguments.delta`;
4. `response.function_call_arguments.done`;
5. `response.output_item.done`;
6. `response.completed`.

Text output keeps the existing text-delta contract. Mixed text/tool output and
parallel calls must retain item indexes and never merge arguments from different
calls. A stream ending mid-arguments is an error, not a completed tool item.

## 6. Conformance and observability

The definition of done is wire-level:

- pure translator tests for request, response and event ordering;
- endpoint integration tests with a fake chat upstream;
- stock `openai` SDK and OpenAI Agents SDK canaries against the in-process app;
- usage, budget reservation, errors and request attribution equal to native
  Responses calls.

Framework-specific branches remain forbidden. The SDKs are canaries; the wire
contract is the specification.

## 7. Provider blockers after Phase 1b-B

- **Bedrock boundaries:** [Converse ToolChoice](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolChoice.html)
  has no general equivalent for `tool_choice=none` or
  `parallel_tool_calls=false`; forcing a named tool is documented only for
  Claude 3 and Nova. Those supported combinations are shipped; everything else
  stays fail-closed. Bedrock's
  [structured-output contract](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
  is model-specific, so `strict=true` and `json_schema` are not inferred from
  family name alone.
- **Vertex:** [Gemini thinking models](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures)
  attach opaque `thought_signature` metadata
  to function-call parts and require exact replay. The normalized Responses
  function-call item has no carrier for it, so generic Vertex tool emulation
  remains 501 rather than using the provider's degraded validator bypass.
  Direct Chat is shipped for validated Gemini 2.5/3 text models through
  `tool_calls[].extra_content.google.thought_signature`, with byte-exact
  replay, ordered parallel results and mandatory Gemini 3 step signatures.

## 8. Non-goals

- Emulating provider-native reasoning, web/file search, stored conversations or
  multimodal items when the selected chat protocol cannot carry them faithfully.
- Persisting `previous_response_id` state inside the gateway.
- Translating across provider-native wire protocols.
