# Native Gemini generateContent endpoint

The gateway exposes Gemini's **native** `generateContent` wire protocol at
`POST /v1beta/models/{model}:generateContent` (and
`:streamGenerateContent` for SSE). Point the stock `google-genai` SDK at the
gateway `base_url` and everything the OpenAI-compatible surface can't express on
Gemini — function calling, function-response turns, multimodal input — passes
through **natively**, while the gateway keeps enforcing the same governance it
applies to `/v1/chat/completions` (auth, per-team budget, usage metering, rate
limiting).

The request's `model` is your **team model alias** (resolved to a Vertex-backed
model), never the upstream `provider_model_id` and never a raw Gemini endpoint.
Gemini's wire shape carries the model in the URL **path**, so the SDK sends it
there. A non-Vertex model behind this endpoint is rejected with `400`.

## Point the SDK at the gateway

Authenticate with your **gateway team API key** (`lsk_...`), not a Gemini key —
the gateway uses its own stored Vertex credential upstream. The `google-genai`
SDK's `api_key=...` is sent as the `x-goog-api-key` header, which the gateway
accepts.

```python
import os

from google import genai
from google.genai.types import HttpOptions

client = genai.Client(
    # Your gateway team key (lsk_...); sent as x-goog-api-key.
    api_key=os.environ["GATEWAY_API_KEY"],
    # The gateway root; the SDK appends `/v1beta/models/<alias>:generateContent`.
    http_options=HttpOptions(base_url="https://your-gateway.example.com"),
)

response = client.models.generate_content(
    model="my-gemini-alias",  # your team model alias
    contents="Hello, Gemini!",
)
print(response.text)
```

Streaming works the same way — the gateway relays the raw Gemini
`GenerateContentResponse` chunks as `data:` Server-Sent Events (the `?alt=sse`
format the SDK parses), untranslated:

```python
for chunk in client.models.generate_content_stream(
    model="my-gemini-alias",
    contents="Write a haiku about the sea.",
):
    print(chunk.text, end="", flush=True)
```

## Function calling (full round-trip)

Function declarations pass through natively, so the standard Gemini tool loop
works unchanged: the model returns a `functionCall` part, you run the tool and
send back a `functionResponse`, and the model produces its final answer.

```python
import os

from google import genai
from google.genai.types import GenerateContentConfig, HttpOptions, Tool

client = genai.Client(
    api_key=os.environ["GATEWAY_API_KEY"],
    http_options=HttpOptions(base_url="https://your-gateway.example.com"),
)

weather_tool = Tool(
    function_declarations=[
        {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
)
config = GenerateContentConfig(tools=[weather_tool])

# Turn 1 — the model asks for the tool.
first = client.models.generate_content(
    model="my-gemini-alias",
    contents="What is the weather in Paris?",
    config=config,
)
call = first.candidates[0].content.parts[0].function_call

# Run your tool with call.args, then feed the result back.
contents = [
    {"role": "user", "parts": [{"text": "What is the weather in Paris?"}]},
    first.candidates[0].content,  # the model's functionCall turn
    {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "get_weather",
                    "response": {"weather": "18C and sunny"},
                }
            }
        ],
    },
]

# Turn 2 — with the function result in context, the model answers.
final = client.models.generate_content(
    model="my-gemini-alias", contents=contents, config=config
)
print(final.text)
```

Every call above is metered against your team budget on the native
`usageMetadata.promptTokenCount` / `usageMetadata.candidatesTokenCount` counts
and shows up under `/teams/{id}/usage` like any other request.

## Errors

Domain errors (unknown model → `404`, budget exceeded → `402`, bad request →
`400`, etc.) return the gateway's OpenAI-shaped error body —
`{"error": {"message", "type", "code"}}` — not Gemini's native
`{"error": {"code", "message", "status"}}` envelope. The HTTP status is
always correct, so status-based SDK error handling (retry/backoff on
`429`/`5xx`, `google.genai.errors.APIError.code` checks) works unchanged;
only a client that additionally inspects the response body should key on
`error.type` / `error.code` rather than assuming the native shape.

## Notes and limits

- **Native passthrough only.** Apart from the documented governance controls
  (reserved-kwarg rejection and output-token ceilings), the Gemini body flows
  upstream unchanged and the native response is returned verbatim. There is no
  OpenAI translation on this endpoint.
- **Vertex models only.** The alias must resolve to a Vertex-backed model;
  otherwise the request is rejected with `400`.
- **No smart routing.** The alias resolves to one concrete Vertex model; a
  router alias on this endpoint is not supported.
- **Server-side credentials.** The gateway calls Vertex with its own stored
  project/location/credential; client-supplied transport settings never change
  how the gateway calls upstream.

See the design rationale in
[Provider-native endpoints](next-steps/native-provider-endpoints.md).
