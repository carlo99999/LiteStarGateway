# Implementation prompt: Agent framework compatibility

> **Status — largely delivered.** Compatibility is achieved by conforming to the
> standard wire protocols (see the framework-agnostic conformance suite in
> `tests/conformance/` and the user guide **[OpenAI-compatible API](../openai-compatible.md)**),
> not by per-framework code. A customer can point Pydantic AI, LangChain,
> LlamaIndex, the OpenAI Agents SDK, or any OpenAI-compatible client at the gateway
> and keep the gateway guarantees: API keys, team scoping, budget enforcement, usage
> metering, model routing, audit, rate limiting, provider abstraction, observability.

This is deliberately different from "build agents inside the gateway." The
gateway remains the governed model backend. Agent loops, planner/executor
patterns, memory, tool implementations, and business-specific orchestration stay
in the caller's framework unless a later product decision explicitly introduces a
managed agent runtime.

## Goal

Make the gateway boringly compatible with common agent frameworks.

Users should be able to configure their framework's OpenAI-compatible settings:

```text
base_url = "https://gateway.example.com/v1"
gateway_token = "<gateway token>"
model    = "<team model name or router alias>"
```

and get working:

- normal chat calls;
- streaming;
- tool/function calling;
- tool-result turns;
- structured outputs / JSON schema;
- embeddings for RAG;
- router aliases as model names where the OpenAI-shaped protocol can support the
  requested capability;
- consistent OpenAI-shaped errors;
- usage and budget accounting for every model call the framework makes.

## Non-goals

- No internal multi-agent runtime in this phase.
- No gateway-owned long-running agent state, memory, planner, task queue, or
  tool registry.
- No cross-protocol emulation that silently drops capability. If a framework
  sends OpenAI-shaped tool calls to a provider path that cannot faithfully carry
  them, fail clearly instead of returning a degraded answer.
- No framework-specific hacks in the request path. Framework support should be
  proven by conformance tests and examples, not by branching on user-agent names.

## Compatibility levels

Use explicit levels instead of a vague "OpenAI-compatible" claim.

| Level | Name | Required surface | What it enables |
| --- | --- | --- | --- |
| A | Chat agent compatible | `/v1/chat/completions`, streaming, tools, tool result messages, `response_format`, usage, embeddings | Most LangChain, Pydantic AI, LlamaIndex, and custom OpenAI-compatible agent loops |
| B | Responses agent compatible | `/v1/responses`, response-item streaming, structured output, tool-call events, usage | OpenAI Agents SDK and frameworks that prefer the Responses API |
| C | Provider-native agent compatible | Native Anthropic/Gemini endpoints with native SDK `base_url` support | Full Claude/Gemini tool, multimodal, caching, and thinking semantics without lossy OpenAI translation |

Level A is the first priority. It is the broadest compatibility target and keeps
the current gateway shape intact. Level B is useful but should not be considered
complete until Responses streaming and tool events are faithfully represented.
Level C is covered by `docs/next-steps/native-provider-endpoints.md` and exists
for provider features that OpenAI-shaped APIs cannot express safely.

## Framework matrix

Track support with a small compatibility matrix in docs and tests:

The user-facing matrix and per-framework config snippets now live in
**[docs/agent-frameworks.md](../agent-frameworks.md)**. Compatibility is proven
at the **protocol** level, not per framework:

| Capability | Status | Proof |
| --- | --- | --- |
| Chat, streaming, tools + tool-result, structured output, embeddings, router alias, OpenAI errors | delivered | `tests/conformance/` (real `openai` SDK vs the in-process app) |
| Native Anthropic / Gemini passthrough | delivered | `tests/conformance/test_native_*.py` |

Every framework in the guide (Pydantic AI, LangChain, LlamaIndex, OpenAI Agents
SDK, custom OpenAI clients) is layered on this protocol and inherits it by
pointing `base_url`/`api_key`/`model` at the gateway — so the executable proof
is the shared protocol conformance suite plus copy-paste config, not a
per-framework test matrix (which would only re-test the frameworks' own OpenAI
adapters). The Agents SDK's Responses-API path is the remaining Level-B fidelity
work.

## Conformance suite

Add framework-level tests that run against the gateway HTTP surface, not only
adapter unit tests.

Each supported framework should have an example under `examples/agents/<name>/`
and a matching test fixture that exercises:

1. simple prompt → text response;
2. streaming prompt → streamed response chunks/events;
3. tool call → framework executes a local test tool → tool result turn → final
   response;
4. structured output → typed object / JSON schema validation succeeds;
5. embeddings → framework obtains embeddings through `/v1/embeddings`;
6. router alias → `model="auto"` or another virtual model routes and bills as
   expected;
7. failure shape → invalid model, invalid tool capability, and budget exceeded
   errors are understandable to the framework.

Prefer fake upstream providers in tests so the conformance suite is deterministic
and does not require external API keys.

## OpenAI-shaped requirements

Level A compatibility depends on getting these details right:

- preserve `tools` and `tool_choice` exactly where the backing provider supports
  them;
- preserve stable tool call IDs;
- accept follow-up messages with `role="tool"` and matching `tool_call_id`;
- stream tool-call deltas in the shape OpenAI clients expect;
- support `response_format={"type":"json_schema", ...}` consistently across
  providers that claim structured-output support;
- expose unsupported protocol/provider combinations as clear 501-style gateway
  errors, not silent parameter drops;
- meter and budget every call, including calls made by framework-internal agent
  loops;
- keep router decisions, usage events, and traces correlated enough to debug a
  multi-call agent run.

The gateway should not need to know that the caller is Pydantic AI or LangChain.
If the wire protocol is correct, framework compatibility follows naturally.

## Level B — remaining Responses fidelity

The Level A and provider-native contracts are delivered. The remaining Level B
work is tracked in
[`docs/next-steps/responses-level-b.md`](responses-level-b.md) and
[`plans/09-responses-level-b.md`](../../plans/09-responses-level-b.md):

The fail-loud capability gate is shipped: every Responses field the
chat-emulation path cannot represent is rejected before budget admission and
provider dispatch. Remaining work:

1. add function tools, `function_call` output items, `function_call_output` input
   and the ordered streaming argument/item events;
2. keep multimodal, stored/stateful, built-in-tool and reasoning features at a
   precise 501 until a faithful mapping exists.

## Documentation deliverables

Add a user-facing "Agent frameworks" page with copy-paste snippets:

```python
# Pydantic AI / OpenAI-compatible provider
base_url = "https://gateway.example.com/v1"
gateway_token = "gw_..."
model = "auto"
```

```python
# LangChain ChatOpenAI
base_url = "https://gateway.example.com/v1"
gateway_token = "gw_..."
model = "auto"
```

```python
# OpenAI client / OpenAI Agents SDK style
base_url = "https://gateway.example.com/v1"
gateway_token = "gw_..."
```

The examples should explain that `model` can be a real team model or a router
alias, and that all calls still go through gateway governance.

## Relationship to provider-native endpoints

Agent framework compatibility has two tracks:

- **OpenAI-compatible track:** maximize Level A and B compatibility for
  frameworks configured with OpenAI-shaped clients.
- **Provider-native track:** expose Anthropic/Gemini native endpoints for
  frameworks that can use native provider SDKs and need features that do not
  round-trip through OpenAI-shaped requests.

Do not use provider-native endpoints as an excuse to leave the OpenAI-compatible
agent surface half-working. Most framework users will try `base_url=/v1` first.
But do not emulate provider-native features through OpenAI when the mapping is
lossy; fail clearly and document the native path.

## Suggested slicing

1. **Docs first** — add the compatibility matrix and copy-paste snippets for
   Pydantic AI, LangChain, LlamaIndex, and OpenAI Agents SDK.
2. **Level A conformance harness** — test simple chat, streaming, tools,
   structured output, embeddings, router aliases, and error shapes against the
   real gateway HTTP app with fake upstream providers.
3. **Tool-call hardening** — close gaps found by the harness, especially
   provider paths that currently cannot carry tools faithfully.
4. **Responses Level B** — execute Plan 09: fail-loud gate first, then buffered
   and streaming function-call fidelity with SDK canaries.
5. **Provider-native track** — implement
   `docs/next-steps/native-provider-endpoints.md` for Claude/Gemini users who
   need native semantics.

The success criterion is simple: a user can follow one page of docs, point their
agent framework at the gateway, and run a small tool-using agent without knowing
anything about the gateway internals.
