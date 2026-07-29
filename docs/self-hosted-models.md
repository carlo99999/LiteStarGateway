# Self-hosted and OpenAI-compatible models

The `openai_compatible` provider points the gateway at **any endpoint that
speaks the OpenAI wire protocol** — a model server you run yourself (vLLM,
Ollama, Text Generation Inference, llama.cpp, LM Studio) or a hosted service
that exposes an OpenAI-shaped API.

It uses the same official `openai` SDK and the same adapter that serves OpenAI
and Databricks, so there is no per-vendor code: everything below applies
identically to every backend.

!!! warning "The gateway guarantees the protocol, not the backend"
    A backend that implements Chat Completions incompletely will surface its
    own errors through the gateway. The sections on
    [capabilities](#3-declare-what-the-model-serves) and
    [known limits](#known-limits) exist because compatibility is a spectrum.

## 1. Authorize the endpoint (required)

Unlike every other provider, these endpoints are normally **private** — a
service on your cluster network. The gateway's usual SSRF guard refuses
private addresses, so it cannot be the check here. An explicit allowlist takes
its place:

```bash
OPENAI_COMPATIBLE_ALLOWED_HOSTS=vllm.internal:8000,10.42.0.0/16,api.example.com
```

Entries are comma-separated and take the form `<target>[:<port>]`, where
`<target>` is a hostname, an IPv4/IPv6 literal, or a CIDR block. An IPv6
target with a port must be bracketed (`[fd00::1]:8000`). Omitting the port
authorizes any port on that target.

Two rules worth knowing:

- **A hostname entry authorizes the name.** You are vouching for that name and
  its DNS; the addresses it resolves to are not separately constrained.
- **A CIDR or literal entry authorizes addresses.** *Every* address the target
  resolves to must fall inside an allowlisted network — one listed address
  cannot smuggle an unlisted sibling through a split DNS answer.

The list is **empty by default**, which means the provider is unusable until
you opt in. Upgrading an existing deployment therefore grants it no new
network reach. Creating a credential for a target that is not allowlisted is
refused with a message naming this variable, and the target is re-resolved and
re-checked on every call, so a name that later drifts out of an allowlisted
range stops working rather than being trusted from config time.

!!! danger "Plaintext endpoints carry the API key in the clear"
    `http://` targets are permitted, because in-cluster plaintext is the
    realistic deployment for a self-hosted server. If the credential carries an
    API key, that key crosses the network unencrypted. Prefer in-cluster TLS or
    a service mesh; the console shows this warning next to the field.

## 2. Create the credential

Platform admin → **Gateway → Credentials → New**, provider
**OpenAI-compatible**, or via the API:

```bash
curl -X POST https://<gateway>/credentials \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "local-vllm",
        "provider": "openai_compatible",
        "values": { "api_base": "http://vllm.internal:8000/v1" }
      }'
```

| Field | Required | Notes |
|---|---|---|
| `api_base` | ✅ | The endpoint, including the `/v1` suffix your server expects. The **only** endpoint source: a team admin cannot redirect it. |
| `api_key` | — | Omit it for a server that needs no auth. The gateway then sends a fixed, non-secret placeholder, because the SDK refuses an empty key. A server that *does* require auth answers 401. |

## 3. Declare what the model serves

One compatible credential may front a chat-only Ollama and another a vLLM that
also serves embeddings, so the gateway cannot infer the operation set from the
provider — and it deliberately never probes your backend to find out. The
model declares it:

```bash
curl -X POST https://<gateway>/teams/$TEAM_ID/models \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "llama-local",
        "provider": "openai_compatible",
        "credential_id": "'"$CREDENTIAL_ID"'",
        "type": "chat",
        "provider_model_id": "meta-llama/Llama-3.1-8B-Instruct",
        "capabilities": ["chat.completions", "embeddings"]
      }'
```

Declarable values are `chat.completions`, `embeddings` and `image_generation`.
Omitting `capabilities` gives you **chat only** — under-declaring makes a model
serve less, never more. Calling an undeclared operation returns `501`, the same
shape as any other unsupported provider/operation pair.

A declaration can only narrow what the adapter offers; it can never add an
operation the adapter does not implement.

Clients then call it like any other model:

```python
from openai import OpenAI

client = OpenAI(api_key="lsk_…", base_url="https://<gateway>/v1")
client.chat.completions.create(
    model="llama-local",
    messages=[{"role": "user", "content": "Hello"}],
)
```

## 4. Pricing and budgets

Set `input_cost_per_token` / `output_cost_per_token` to `0.0` for a model you
host yourself, or to an amortized synthetic rate if you want self-hosted
inference to consume team budgets proportionally. Everything downstream —
the usage ledger, budgets, alerts, usage analytics, cache savings — behaves
exactly as it does for a cloud provider.

## Known limits

- **`n > 1` is refused.** Compatible backends disagree: vLLM honors `n`,
  Ollama ignores it. Claiming support would over-reserve budget on the
  backends that silently return a single completion, so the gateway refuses
  rather than guessing.
- **No Responses API.** Compatible backends implement Chat Completions;
  `POST /v1/responses` against one of these models returns `501`.
- **No native passthrough endpoints** and no upstream model discovery — models
  stay explicitly configured, so the team-visible catalogue is something an
  admin decided.
- **Streamed usage may be estimated.** The gateway forces
  `stream_options.include_usage` so billing never depends on the client, but a
  server free to ignore it leaves no usage chunk. Settlement then estimates
  tokens from the prompt and streamed output rather than billing zero. The
  ledger does not currently mark which rows were estimated.
- **Tool calling passes straight through.** This is a passthrough, not a
  translator, so tools work exactly as well as your backend supports them; a
  backend that cannot do them answers `400`.

## Verified backends

The gateway targets the protocol, not a list — but these are known to work
with the configuration above:

| Backend | `api_base` shape | Notes |
|---|---|---|
| vLLM | `http://host:8000/v1` | Chat + embeddings; honors `n` |
| Ollama | `http://host:11434/v1` | Chat; no API key needed |
| TGI | `http://host:8080/v1` | Chat |
| LM Studio | `http://host:1234/v1` | Chat; local development |

Hosted OpenAI-compatible services work the same way — allowlist the public
hostname and supply the API key.
