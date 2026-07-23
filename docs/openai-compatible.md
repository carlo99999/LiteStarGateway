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
Azure OpenAI, Databricks, Anthropic, validated Vertex Gemini 2.5/3 text models,
and capability-gated AWS Bedrock Claude 3/Nova models**. Anthropic, Vertex and
Bedrock support faithful non-streaming definitions, exact call IDs and
tool-result replay. Anthropic maps `strict`; Bedrock accepts only
omitted/`false` strict intent for its validated matrix.

Vertex maps OpenAI function declarations and tool choices to Gemini and
preserves the provider's opaque state through
`tool_calls[].extra_content.google.thought_signature`. Append the returned
assistant message unchanged before the correlated tool messages; the stock
OpenAI SDK preserves this provider extension in `model_extra` and
`model_dump()`. Gemini 3 replay requires the signature on the first call of
each step. If Vertex omits a call ID, the gateway-generated ID carries
`extra_content.litestar_gateway.synthetic_call_id=true`; preserve that marker
too so replay does not add an ID to the signed Gemini part. Vertex validates
function and parameter names against Google's limits and rejects explicit
`strict`, `parallel_tool_calls=false`, streaming tools, non-text/unvalidated
model families and schemas outside the documented subset before routing.

Bedrock maps `auto`, `required → any`, and supported named choices. It rejects
`strict=true`, `tool_choice=none`, `parallel_tool_calls=false`, opaque model ARNs
and unknown model families before routing because the selected Converse model
matrix cannot prove those contracts. Amazon Nova schemas are additionally
limited to a top-level object containing only `type`, `properties` and
`required`.
Translated streaming tool calls, generic Vertex `/v1/responses` tool loops, and
vision content on these translated adapters remain `501 Not Implemented`
rather than silently degrading. Vertex Responses has no normalized carrier for
the thought signature, so the gateway does not use Google's degraded validator
bypass. For unrestricted Anthropic and Gemini features, use the native
endpoints below; Bedrock has no gateway-native endpoint.

## Errors

Errors on `/v1/*` use the OpenAI error envelope, so SDK error handling works as
expected:

```json
{ "error": { "message": "...", "type": "invalid_request_error", "code": "ModelNotFound" } }
```

## Which surface should I use?

| Use the **OpenAI-compatible** surface (`/v1/chat/completions`) | Use a **native** endpoint |
|---|---|
| Provider-agnostic or OpenAI-targeted clients/agents, including bounded Gemini Chat tool loops | Anthropic or Gemini agents that need native multimodal, thinking or unrestricted tool semantics |
| Text and non-streaming OpenAI/Azure/Databricks/Anthropic/Vertex/capability-gated Bedrock tool calling | Provider-specific features the OpenAI shape can't express |
| You want one client shape across many providers | You already use the native `anthropic` / `google-genai` SDK |

Native endpoints:

- [Native Anthropic endpoint](native-anthropic.md) — `POST /v1/messages`
- [Native Gemini endpoint](native-gemini.md) — `POST /v1beta/models/{model}:generateContent`

Both authenticate with the same gateway team key, meter natively, and pass the
provider's wire format through untranslated.
