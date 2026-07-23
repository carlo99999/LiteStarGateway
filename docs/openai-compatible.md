# OpenAI-compatible API

The gateway's primary surface implements the **OpenAI Chat Completions wire
protocol**. Point the stock `openai` client — or *any* framework that speaks that
protocol — at the gateway with a team API key; no gateway-specific client, no
per-framework setup.

```python
from openai import OpenAI

client = OpenAI(api_key="lsk_...", base_url="https://<host>/v1")
resp = client.chat.completions.create(
    model="<team-model-alias>",   # your team's model alias, not a raw provider id
    messages=[{"role": "user", "content": "Hello"}],
)
```

The `model` is a **team model alias** the platform admin configured; the gateway
resolves it to the real provider + credential (the upstream key and `base_url` stay
server-side and are never accepted from the client).

## Framework-agnostic by contract

Compatibility is guaranteed by conforming to the protocol, not by supporting named
frameworks. Anything layered on the OpenAI Chat Completions protocol works
unchanged — for example:

- the official **OpenAI** Python/JS clients and the **OpenAI Agents SDK**;
- **Pydantic AI** with an OpenAI-compatible provider;
- **LangChain** `ChatOpenAI`.

None of these need gateway-specific code. The wire contract (request/response,
tool-call shapes, streaming deltas, error envelope) is locked by an automated
conformance suite (`tests/conformance/`) driven by the official `openai` SDK, so a
regression breaks a contract test rather than surfacing in your agent.

## Tool calling

Tool/function calling over this surface works for models backed by **OpenAI,
Azure OpenAI, Databricks, Anthropic, and capability-gated AWS Bedrock Claude
3/Nova models**. Anthropic and Bedrock support faithful non-streaming
definitions, exact call IDs and tool-result replay. Anthropic maps `strict`;
Bedrock accepts only omitted/`false` strict intent for this validated matrix.

Bedrock maps `auto`, `required → any`, and supported named choices. It rejects
`strict=true`, `tool_choice=none`, `parallel_tool_calls=false`, opaque model ARNs
and unknown model families before routing because the selected Converse model
matrix cannot prove those contracts. Amazon Nova schemas are additionally
limited to a top-level object containing only `type`, `properties` and
`required`.
Anthropic/Bedrock streaming tool calls, all translated tool calls on **Vertex
(Gemini)**, and vision content on these translated adapters remain `501 Not
Implemented` rather than silently degrading. For unrestricted Anthropic and
Gemini features, use the native endpoints below; Bedrock has no gateway-native
endpoint.

## Errors

Errors on `/v1/*` use the OpenAI error envelope, so SDK error handling works as
expected:

```json
{ "error": { "message": "...", "type": "invalid_request_error", "code": "ModelNotFound" } }
```

## Which surface should I use?

| Use the **OpenAI-compatible** surface (`/v1/chat/completions`) | Use a **native** endpoint |
|---|---|
| Provider-agnostic or OpenAI-targeted clients/agents | Anthropic or Gemini tool-calling agents that need full fidelity |
| Text and non-streaming OpenAI/Azure/Databricks/Anthropic/capability-gated Bedrock tool calling | Provider-specific features the OpenAI shape can't express |
| You want one client shape across many providers | You already use the native `anthropic` / `google-genai` SDK |

Native endpoints:

- [Native Anthropic endpoint](native-anthropic.md) — `POST /v1/messages`
- [Native Gemini endpoint](native-gemini.md) — `POST /v1beta/models/{model}:generateContent`

Both authenticate with the same gateway team key, meter natively, and pass the
provider's wire format through untranslated.
