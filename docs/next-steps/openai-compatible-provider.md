# Design doc — Generic OpenAI-compatible provider

> **Status:** proposed. The gateway ships six closed provider adapters, so a
> self-hostable gateway cannot reach a self-hosted model server (vLLM, Ollama,
> TGI, llama.cpp) nor any of the OpenAI-compatible SaaS endpoints (Groq,
> Mistral, DeepSeek, Together, Fireworks, xAI, OpenRouter). Execution plan:
> [`plans/18-openai-compatible-provider.md`](../../plans/18-openai-compatible-provider.md).

## 1. The gap, and why it is not "sprawl"

`README.md` states the principle: *focused over sprawling — a curated set of
providers, done right, instead of a long tail of half-supported ones*. That
principle is about **bespoke adapters**, and this design does not add one.

`infrastructure/llm/openai_adapter.py` already contains
`OpenAICompatibleAdapter`, a vendor-neutral implementation of all four
operations over the official `openai` SDK, and `_base_url()` already reads
`api_base` from the credential. Databricks is *already* served by it —
`OpenAIAdapter`'s own docstring says "plain OpenAI, and OpenAI-compatible
endpoints (e.g. Databricks via base_url)". What is missing is not an adapter:
it is a `Provider` value, a credential contract, a per-model capability
declaration, and an egress policy.

So the scope is **one** provider value backed by the **existing** adapter, and
the hard rule below keeps it that way.

> **The no-quirks rule.** The `openai_compatible` adapter must contain zero
> vendor branches — no `if "groq" in base_url`, no per-vendor response
> fix-ups, ever. A backend that needs special-casing is by definition not
> OpenAI-compatible, and belongs behind its own `Provider` value with its own
> official SDK, exactly like Anthropic or Vertex. A PR that adds a vendor
> branch here is the signal that the boundary was crossed.

## 2. Provider value and credential contract

`Provider.OPENAI_COMPATIBLE = "openai_compatible"`. `ModelRecord.provider` and
`CredentialRecord.provider` are plain `Mapped[str]` columns
(`infrastructure/persistence/orm.py:731`, `:907`) with no database-level enum,
so the new value needs **no migration** of its own.

Credential fields (`domain/credential_policy.py`):

| key | required | notes |
|---|---|---|
| `api_base` | ✅ | the only endpoint source; validated against the egress allowlist (§4) |
| `api_key` | — | optional: local servers (Ollama, llama.cpp) accept none |

`api_key` is optional but the `openai` SDK rejects an empty one, so
`require_api_key` cannot be reused. When absent the adapter sends a fixed
non-secret placeholder. This is a documented behavior, not a fallback that
might silently mask a misconfigured secret: an endpoint that *does* require
auth answers 401, which `errors.py` already translates.

Endpoint stays credential-side (platform-admin-managed) for the reason already
recorded on `Model` (`domain/entities/model.py:41`): a team admin must never be
able to redirect a credential's secret at a host of their choosing.

## 3. Capabilities are declared, never detected

`LLMGatewayImpl._registry` maps a provider to a static operation set. That
cannot work here: one `openai_compatible` credential may front a chat-only
Ollama, another a vLLM server that also serves embeddings.

Add a `capabilities` set on **`Model`** — a subset of
`{chat.completions, embeddings, image_generation}` — and have `_resolve` take
the model rather than `model.provider`, intersecting the provider's maximum set
with the model's declaration. An undeclared operation raises the existing
`UnsupportedOperation` → 501, identical in shape to today's
"Provider 'anthropic' does not support 'embeddings'".

Two decisions worth stating explicitly:

- **Declared, not probed.** The gateway never calls upstream to discover what
  a backend supports. Detection is a cache-invalidation problem and a startup
  dependency on a third-party host; a wrong declaration costs a clear 501 or a
  translated upstream 400, which is an operator's problem to fix once.
- **Model-level, not credential-level.** Capabilities are not secrets and the
  console must read them without decrypting. Putting them on `Model` makes them
  team-editable, which is acceptable *because a false declaration has no
  security consequence* — it degrades to an upstream error. The
  security-sensitive field (the endpoint) stays on the credential. The default
  is chat-only, so an under-declared model fails closed.

`ModelType` is unchanged: it already says what a model *is*; `capabilities`
says which gateway operations it will serve.

## 4. Egress policy (the security core)

Every other `api_base` today points at a vendor's public cloud. Here, a private
address is the *normal* case — which means this feature deliberately turns the
gateway into a server-side request forwarder aimed at the internal network.

The existing SSRF guard is the wrong tool, and inverting it is the whole point.
`application/routing/webhook.py:71` `resolve_approved_addresses()` enforces a
deny-list — "anything that isn't a plain public unicast address" — which would
reject `http://vllm.svc.cluster.local:8000`, the canonical use case.

The policy instead is a platform allowlist:

```text
OPENAI_COMPATIBLE_ALLOWED_HOSTS=vllm.internal:8000,10.42.0.0/16,api.groq.com
```

- **Empty by default ⇒ the provider is unusable.** An existing deployment that
  upgrades gains no new egress reach until an operator opts in. Credential
  creation with an empty allowlist fails with a message naming the setting.
- **Validated on write and re-resolved on every call**, mirroring the
  anti-DNS-rebinding property already documented on
  `resolve_approved_addresses` — with the allow/deny sense flipped. The
  matching helper belongs in `domain/` next to the rest of the policy code, and
  the two guards should share their address-resolution helper rather than
  re-implement it.
- **`http://` is permitted only for allowlisted hosts**, because in-cluster
  plaintext is the realistic deployment. The API key then crosses the network
  in clear; the console and docs must say so where the field is entered, and
  recommend mesh/in-cluster TLS.

Creating one of these credentials is a platform-admin action and lands in the
append-only audit trail like every other credential write.

## 5. Hot-path details that are easy to get wrong

- **`n` stays unsupported.** `Provider.honors_n` exists for budget correctness
  (R7-M50: requesting `n>1` where the provider silently returns one completion
  over-reserves budget by up to MAX_N×). OpenAI-compatible backends disagree —
  vLLM honors `n`, Ollama does not — so the new value is **not** added to
  `_PROVIDERS_HONORING_N` and `n>1` is rejected. Promoting it to a declared
  capability later is a clean follow-up; guessing now is a billing bug.
- **Streaming usage may be absent.** `astream_chat_completion` forces
  `stream_options.include_usage` so billing never depends on the client. Many
  compatible servers ignore that field. Metering then falls through to
  `UsageMeter.metered_stream`'s estimated-tokens path (prompt text + streamed
  output at ~4 chars/token) rather than billing zero. That fallback sits
  *above* the adapter, so it is one shared behaviour every provider inherits,
  already covered by `tests/completions/test_stream_usage_fallback.py` and now
  pinned for this provider too.

  What does **not** exist — corrected from an earlier draft of this section —
  is any persisted marker distinguishing an estimated row from an
  authoritative one. `UsageEvent` has no such field, so an operator cannot
  query which spend was estimated. On the six existing providers that is a
  rare edge (a disconnect, a refusal); on a self-hosted backend that ignores
  `include_usage` it is the *normal* settlement path, which makes the gap
  materially worse here. Adding the flag is a ledger column and belongs with
  Plan 13 (billing integrity) or Plan 10's estimated-vs-authoritative counts,
  not to this plan — but it should be raised there rather than left implicit.
- **Client pooling must not alias.** `OpenAIAdapter._client_key` hardcodes
  `provider="openai"`. The new subclass must tag its `ClientKey` with
  `openai_compatible` so a pooled client is never shared across two providers
  that happen to share an endpoint and credential fingerprint.
- **Tools work by construction.** This is a passthrough, not a translator, so
  `ensure_translatable_chat_request` does not apply and tool calls reach
  upstream verbatim. A backend that cannot do tools answers 400, translated by
  `errors.py`. That is the honest behavior: the gateway does not claim tool
  support it cannot verify, and does not block it where it works.
- **Pricing keeps working unchanged.** `Model.input_cost_per_token` /
  `output_cost_per_token` are per-model already, so a self-hosted model is
  configured at `0.0` (or an amortized synthetic rate) and budgets, the ledger,
  usage analytics and cache-savings all behave exactly as they do today.

### 6. Non-goals

- **No Responses API.** `openai_compatible` is not added to
  `_EMULATED_RESPONSES_TOOL_PROVIDERS` or the streaming tool sets; `responses`
  is not in its capability set. Compatible backends implement Chat Completions;
  Responses coverage is Plan 09's contract, not this one's.
- **No native passthrough endpoints** (no `/v1/messages`-style surface).
- **No upstream model discovery.** The gateway will not populate models by
  reading a backend's `GET /v1/models`. Models stay explicitly configured, so
  the team-visible catalogue remains something an admin decided.
- **No vendor presets.** No dropdown of "Groq / Together / …" pre-filling
  base URLs — that is the first step toward the quirks the no-quirks rule
  forbids. Documentation may list known-good endpoints; code must not.
- **No new pricing catalogue** for third-party vendors.
