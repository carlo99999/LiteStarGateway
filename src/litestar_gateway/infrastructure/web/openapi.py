"""OpenAPI presentation copy for the built-in documentation viewers."""

OPENAPI_DESCRIPTION = """
# Litestar Gateway

**One governed entry point for LLM traffic.**

Litestar Gateway centralizes OpenAI-compatible inference, model deployments,
router aliases, provider credentials, budget enforcement, usage metering, audit,
and observability.

Use a real team model name or a router alias in the `model` field. The gateway
applies the same authentication, rate limits, budget checks, and usage
accounting in both cases.

## Supported surfaces

- Chat completions and streamed chat completions
- Responses API, including emulated provider support where available
- Embeddings and image generation
- Structured outputs / JSON schema
- Smart routing, shadow routing, and weighted routing
- Team-scoped models, credentials, API keys, budgets, usage, and audit logs

## Documentation viewers

| Viewer | Best for | Link |
| --- | --- | --- |
| Swagger UI | Quick endpoint exploration and request testing | [Open Swagger UI](/) |
| Scalar | Clean API reference browsing | [Open Scalar](/scalar) |
| Stoplight Elements | Contract review and navigation | [Open Elements](/elements) |
| OpenAPI JSON | SDK generation, tests, and tooling | [Download schema](/openapi.json) |
""".strip()
