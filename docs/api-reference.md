# API reference (interactive)

A running gateway serves its own **interactive OpenAPI documentation** — generated
from the live route handlers, so it always matches the deployed version. Point a
browser at the instance's base URL (`https://<host>/`) to explore and try the API.

These viewers are enabled by the OpenAPI config in
`src/litestar_gateway/app.py` (`_build_openapi_config`) and are public only while
`OPENAPI_ENABLED` is on. Operators set `OPENAPI_ENABLED=false` in production to keep
the full API surface unexposed.

## Viewers and schema

| Path            | What it serves                                                    |
| --------------- | ----------------------------------------------------------------- |
| `/`             | **Swagger UI** — the default landing page; browse and try requests |
| `/scalar`       | **Scalar** — an alternative reference renderer                     |
| `/elements`     | **Stoplight Elements** — another alternative renderer              |
| `/openapi.json` | The raw **OpenAPI schema** (machine-readable; feed it to codegen, Postman, etc.) |

All four are served from the same generated schema, so they describe the identical
API surface — pick whichever viewer you prefer.

## Narrative docs

This site — the narrative documentation you are reading — is also served by a
running instance at **`/docs`**, alongside the interactive API viewers above. See
[OpenAI-compatible API](openai-compatible.md) for how to call the gateway with the
stock `openai` client.
