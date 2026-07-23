# Design doc — Responses API Level B

> **Status:** Phase 0 shipped. `/v1/responses` preserves the governed,
> synchronous and stateless OpenAI SDK request surface for OpenAI/Azure and
> supports text + structured-output emulation over chat-only providers.
> Unsupported fields now fail with 501 before router side effects, budget
> admission or provider dispatch. The remaining contract gap is faithful
> tool/function-call items and streaming events on chat-only providers.
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
- `function_call_output` input → a `role="tool"` message with the matching
  `tool_call_id`.

The reverse mapping turns chat `message.tool_calls` into Responses
`function_call` output items. Preserve provider-issued IDs when present; generate
stable response item IDs only when the chat protocol does not supply one.

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

## 7. Non-goals

- Emulating provider-native reasoning, web/file search, stored conversations or
  multimodal items when the selected chat protocol cannot carry them faithfully.
- Persisting `previous_response_id` state inside the gateway.
- Translating across provider-native wire protocols.
