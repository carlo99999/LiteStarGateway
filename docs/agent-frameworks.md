# Agent frameworks

The gateway is a **governed model backend** for agent frameworks, not an agent
runtime. Point your framework's OpenAI-compatible settings at the gateway and
keep every gateway guarantee — API-key auth, team scoping, budgets, rate
limits, usage metering, smart routing, audit — while your agent loop, tools,
memory, and orchestration stay in your framework.

```text
base_url = "https://<host>/v1"
api_key  = "lsk_…"          # a team API key
model    = "<team model alias or router name>"
```

Compatibility comes from speaking the **OpenAI wire protocol** faithfully (see
[OpenAI-compatible API](openai-compatible.md)) — there is no per-framework code
in the gateway. The matrix below is backed by an executable conformance suite
(`tests/conformance/`) that drives the real `openai` SDK against the in-process
app, so a green cell is a passing test, not a claim.

## Compatibility matrix

| Capability | Status | Proof (`tests/conformance/`) |
| --- | --- | --- |
| Chat completions | ✅ | `test_response_envelope.py` |
| Streaming (SSE) | ✅ | `test_streaming.py` |
| Tool / function calling (+ tool-result turn) | ✅ | `test_tool_calling.py` |
| Structured output (`response_format`) | ✅ | `test_structured_output.py` |
| Embeddings (RAG) | ✅ | `test_embeddings.py` |
| Router alias as `model` | ✅ | `test_router_alias.py` |
| OpenAI-shaped errors | ✅ | `test_errors.py` |
| Native Anthropic / Gemini passthrough | ✅ | `test_native_anthropic.py`, `test_native_gemini.py` |

Any framework layered on the OpenAI Chat Completions protocol inherits all of
the above. Unsupported protocol/provider combinations fail with a clear
gateway error (e.g. tools on a provider path that can't carry them → 501)
rather than silently degrading.

## Copy-paste configuration

### OpenAI Python / JS SDK

```python
from openai import OpenAI

client = OpenAI(api_key="lsk_…", base_url="https://<host>/v1")
client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "hi"}])
```

### Pydantic AI

```python
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

model = OpenAIModel(
    "auto",  # a team model alias or router name
    provider=OpenAIProvider(base_url="https://<host>/v1", api_key="lsk_…"),
)
agent = Agent(model)
```

### LangChain

```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

llm = ChatOpenAI(model="auto", base_url="https://<host>/v1", api_key="lsk_…")
embeddings = OpenAIEmbeddings(model="<embeddings-alias>", base_url="https://<host>/v1", api_key="lsk_…")
```

### LlamaIndex

```python
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai_like import OpenAILikeEmbedding

llm = OpenAILike(model="auto", api_base="https://<host>/v1", api_key="lsk_…", is_chat_model=True)
embed = OpenAILikeEmbedding(model_name="<embeddings-alias>", api_base="https://<host>/v1", api_key="lsk_…")
```

### OpenAI Agents SDK

```python
from agents import Agent, OpenAIChatCompletionsModel
from openai import AsyncOpenAI

client = AsyncOpenAI(base_url="https://<host>/v1", api_key="lsk_…")
agent = Agent(name="assistant", model=OpenAIChatCompletionsModel("auto", openai_client=client))
```

The `OpenAIChatCompletionsModel` path (chat completions) is fully supported.
The Agents SDK's Responses-API path maps to `/v1/responses` — usable, but its
streaming/tool-event fidelity is the ongoing Level-B work (see below).

## What stays in your framework

Agent loops, planner/executor patterns, memory, tool implementations, and
business orchestration. The gateway deliberately does **not** provide a managed
agent runtime, long-running agent state, or a tool registry — it governs and
meters every model call your framework makes, and gets out of the way for
everything else.

## Levels & scope

- **Level A — chat-agent compatible** (this page): `/v1/chat/completions`,
  streaming, tools, `response_format`, embeddings, router aliases, usage. The
  broadest target; covers most LangChain / Pydantic AI / LlamaIndex / custom
  OpenAI-compatible loops.
- **Level B — Responses-API agent compatible**: `/v1/responses` with faithful
  response items and tool events. Native Responses providers are usable today;
  Databricks, Anthropic and capability-gated Bedrock Claude 3/Nova models also
  support non-streaming function loops. Validated Vertex Gemini 2.5/3 text
  models support byte-exact thought-signature replay through direct Chat, but
  generic Vertex Responses tools remain fail-closed because function-call items
  have no safe signature carrier. Emulated streaming tools remain in progress.
- **Level C — provider-native**: native Anthropic/Gemini endpoints for
  provider features OpenAI-shaped APIs can't express (see
  [OpenAI-compatible API](openai-compatible.md) and the native-endpoint docs).

Design notes and non-goals: `docs/next-steps/agent-framework-compatibility.md`.
