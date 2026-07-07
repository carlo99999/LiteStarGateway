# Design doc — AWS Bedrock provider

> **Status — implemented**: `infrastructure/llm/bedrock_adapter.py` (Converse
> chat + streaming, emulated Responses via `ChatToResponsesAdapter`,
> Titan/Cohere embeddings and Titan images via `invoke_model`, structured
> outputs as a forced tool), botocore error translation in
> `infrastructure/llm/errors.py`, registered in the gateway capability matrix.
> Async surface delegates to a worker thread (`asyncio.to_thread`), streaming
> included. Deferred from this doc: `role_arn` assume-role credentials and
> non-Titan image families.

## 1. Goal

Add AWS Bedrock as a provider, OpenAI-compatible like the others, fitting the
existing adapter/gateway structure.

## 2. Key decision: use the Converse API + boto3 (no hand-rolled SigV4)

The README worry was "SigV4 + event-stream". In practice **boto3 handles SigV4
automatically** from credentials, and the **Bedrock Converse API**
(`bedrock-runtime.converse` / `converse_stream`) gives a unified, model-agnostic
message format very close to OpenAI chat. So we do NOT hand-roll signing.

- **Chat** → `converse` (translate OpenAI chat ↔ Converse messages, pure funcs).
- **Streaming** → `converse_stream` (map stream events → OpenAI chunks, a pure
  mapper like `anthropic_event_to_delta`).
- **Responses** → emulate via the existing `ChatToResponsesAdapter` (Bedrock has
  no Responses API), exactly like Databricks/Anthropic.
- **Embeddings / Images** → `invoke_model` (Titan / Cohere embeddings; Titan
  Image / SD for images), model-family specific bodies.

## 3. Sync + async

boto3 is synchronous. Mirror the existing pattern: implement sync methods
directly and provide async ones via `anyio.to_thread.run_sync` (dependency-light,
no aioboto3). The gateway already exposes sync + async variants.

## 4. Architecture (fits today's structure)

```text
domain/entities.py        Provider.BEDROCK
infrastructure/llm/bedrock_adapter.py
    to_converse_request / from_converse_response / converse_chunk_to_delta
    to_titan_embed_request / from_titan_embeddings
    to_image_request / from_image_response
infrastructure/llm/gateway.py   register BEDROCK ops (chat, responses=emulated,
                                 stream, embeddings, images per model support)
```

Pure translators + thin adapter I/O, identical in spirit to `vertex_adapter.py`.

## 5. Credentials

Encrypted `Credential` (platform-global, like the others). Free-form JSON values:

- `aws_access_key_id`, `aws_secret_access_key`, optional `aws_session_token`;
- `region` (required);
- optional `role_arn` for STS assume-role.

The Bedrock endpoint/region comes only from the credential (SSRF-safe, consistent
with the rule that the team-controlled `Model` never sets the endpoint).

## 6. Open decisions

1. **Auth modes**: static keys only, or also assume-role (STS) / instance profile?
   Start with static keys + optional session token; add role assumption later.
2. **Converse vs InvokeModel for chat**: recommend Converse (uniform). Keep
   `invoke_model` only for embeddings/images.
3. **Async**: boto3 + `to_thread` (recommended) vs adding `aioboto3`.
4. **Model id mapping**: `provider_model_id` = Bedrock modelId (e.g.
   `anthropic.claude-3-5-sonnet-...` or an inference profile ARN).

## 7. Testing

- Pure tests on the translators (OpenAI ↔ Converse, chunk mapping, Titan bodies).
- Adapter tests with a faked boto3 `bedrock-runtime` client (monkeypatch), like
  the other providers' SDK fakes. No real AWS calls.

## 8. Rollout (stacked, like the original provider work)

1. `feat/bedrock-chat` — credential/provider + Converse chat (+ responses
   emulation) + sync/async + tests.
2. `feat/bedrock-streaming` — `converse_stream` mapping.
3. `feat/bedrock-embeddings-images` — `invoke_model` paths.
