# LiteStar Gateway

An OpenAI-compatible LLM gateway (Litestar, hexagonal architecture). Customers
point the stock OpenAI client at this server with a team API key:

```python
from openai import OpenAI
client = OpenAI(api_key="lsk_...", base_url="https://<host>/")
client.chat.completions.create(model="<team-model-alias>", messages=[...])
```

The alias resolves to a team `Model`, which selects the provider and a
platform-managed, encrypted `Credential`. Providers: OpenAI, Azure OpenAI,
Databricks, Anthropic, Vertex/Gemini.

## Endpoints

| Endpoint | OpenAI | Azure | Databricks | Anthropic | Vertex |
|---|:--:|:--:|:--:|:--:|:--:|
| `POST /v1/chat/completions` (+ `stream`) | ✅ | ✅ | ✅ | ✅ | ✅ |
| `POST /v1/responses` (+ `stream`) | native | native | emulated | emulated | emulated |
| `POST /v1/embeddings` | ✅ | ✅ | ✅ | 501 | ✅ |
| `POST /v1/images/generations` | ✅ | ✅ | 501 | 501 | ✅ |

Plus: users/invites, JWT login, organizations → teams → memberships, team-scoped
API keys, and encrypted provider credentials (admin-managed).

## Configuration

See `.env.sample`. Key env vars: `DATABASE_URL`, `MASTER_KEY` (bootstrap admin),
`JWT_SECRET` (login token signing), `SALT_KEY` (credential encryption at rest).

```bash
uv run litestar --app litestar_test.app:app run
uv run pytest
```

## Security — known issues & follow-ups

Tracked items not yet implemented (see also the code review notes):

- **Rate limiting** — there is no throttling on `/v1/*`, `/login`, or `/signup`.
  A production deployment should add per-key / per-IP rate limits to bound abuse
  and provider costs.
- **Email enumeration on signup** — `POST /signup` returns `409` with the email
  when it already exists, revealing which emails are registered. Login is already
  generic; signup should be made non-revealing.
- **Unvalidated request passthrough** — chat/responses requests are forwarded to
  the provider SDKs largely as-is (`{**model.params, **request}`). `model` is
  overridden, but other fields (e.g. `extra_headers`, large `n`) pass through.
  Consider an allowlist of accepted parameters per operation.
- **`JWT_SECRET` dev default** — `config.py` falls back to an insecure default if
  `JWT_SECRET` is unset, so a misconfigured production deploy could use a known
  signing key (forgeable tokens). Consider failing fast when unset in production,
  as is already done for `SALT_KEY`.

### Resolved

- **Credential exfiltration via model `api_base` (SSRF)** — the provider endpoint
  now comes only from the admin-managed credential, never from the team-controlled
  model (`Model.api_base` was removed). A team admin can no longer redirect a
  credential's secret to an arbitrary host.
- **Missing `api_key` → 500** — a credential without `api_key` now returns a clean
  `400` (`CredentialMisconfigured`) instead of an unhandled error.
