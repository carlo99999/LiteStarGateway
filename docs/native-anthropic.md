# Native Anthropic Messages endpoint

The gateway exposes Anthropic's **native** Messages wire protocol at
`POST /v1/messages`. Point the stock `anthropic` SDK at the gateway `base_url`
and everything the OpenAI-compatible surface can't express on Claude — tool
calling, tool-result turns, multimodal input, prompt caching, extended thinking
— passes through **natively**, while the gateway keeps enforcing the same
governance it applies to `/v1/chat/completions` (auth, per-team budget, usage
metering, rate limiting).

The request's `model` is your **team model alias** (resolved to a Claude-backed
model), never the upstream `provider_model_id` and never a raw Anthropic
endpoint. A non-Anthropic model behind this endpoint is rejected with `400`.

## Point the SDK at the gateway

Authenticate with your **gateway team API key** (`lsk_...`), not an Anthropic
key — the gateway uses its own stored Anthropic credential upstream. The
`anthropic` SDK sends the key as the `x-api-key` header, which the gateway
accepts.

```python
import os

from anthropic import Anthropic

client = Anthropic(
    # The gateway root; the SDK appends `/v1/messages`.
    base_url="https://your-gateway.example.com",
    # Your gateway team key (lsk_...), read from the environment.
    api_key=os.environ["GATEWAY_API_KEY"],
)

message = client.messages.create(
    model="my-claude-alias",  # your team model alias
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello, Claude!"}],
)
print(message.content[0].text)
```

Streaming works the same way — the gateway relays the raw Anthropic
Server-Sent Events untranslated:

```python
with client.messages.stream(
    model="my-claude-alias",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Write a haiku about the sea."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

## Tool calling (full round-trip)

Tools pass through natively, so the standard Anthropic tool loop works
unchanged: the model returns a `tool_use` block, you run the tool and send back
a `tool_result`, and the model produces its final answer.

```python
import os

from anthropic import Anthropic

client = Anthropic(
    base_url="https://your-gateway.example.com",
    api_key=os.environ["GATEWAY_API_KEY"],
)

tools = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]

messages = [{"role": "user", "content": "What is the weather in Paris?"}]

# Turn 1 — the model asks for the tool.
first = client.messages.create(
    model="my-claude-alias", max_tokens=1024, tools=tools, messages=messages
)
tool_use = next(block for block in first.content if block.type == "tool_use")

# Run your tool with tool_use.input, then feed the result back.
messages.append({"role": "assistant", "content": first.content})
messages.append(
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": "18C and sunny",
            }
        ],
    }
)

# Turn 2 — with the tool result in context, the model answers.
final = client.messages.create(
    model="my-claude-alias", max_tokens=1024, tools=tools, messages=messages
)
print(final.content[0].text)
```

Every call above is metered against your team budget on the native
`usage.input_tokens` / `usage.output_tokens` counts and shows up under
`/teams/{id}/usage` like any other request.

## Notes and limits

- **Native passthrough only.** The Anthropic body flows upstream unchanged and
  the native response is returned verbatim — there is no OpenAI translation on
  this endpoint. Use `/v1/chat/completions` for the OpenAI-compatible surface.
- **Anthropic models only.** The alias must resolve to a Claude-backed model;
  otherwise the request is rejected with `400`.
- **No smart routing (phase 1).** The alias resolves to one concrete Anthropic
  model; a router alias on this endpoint is not supported.
- **Transport overrides are stripped.** Client-supplied `base_url` / `api_key`
  overrides never change how the gateway calls upstream with its own credential.

See the design rationale in
[Provider-native endpoints](next-steps/native-provider-endpoints.md).
