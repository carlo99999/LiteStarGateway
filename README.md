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
- **Cross-team credential usage (by design)** — credentials are platform-global,
  so any team admin can reference any credential in a model and consume it (they
  cannot read its secret). This is intentional for now; tie credentials to a
  team/org if per-team isolation becomes a requirement.
- **SQLite for dev/test** — the default is file SQLite (single-writer, weak
  concurrency). Production should use Postgres (`postgresql+asyncpg://…`).

### Resolved

- **Credential exfiltration via model `api_base` (SSRF)** — the provider endpoint
  now comes only from the admin-managed credential, never from the team-controlled
  model (`Model.api_base` was removed). A team admin can no longer redirect a
  credential's secret to an arbitrary host.
- **Missing `api_key` → 500** — a credential without `api_key` now returns a clean
  `400` (`CredentialMisconfigured`) instead of an unhandled error.
- **Invite single-use race (TOCTOU)** — invites are consumed with an atomic
  conditional `UPDATE … WHERE used_at IS NULL`, so concurrent signups can't reuse
  one invite.
- **`last_used_at` write on every request** — the API-key auth hot path now
  persists `last_used_at` at most once per minute per key.
- **No token revocation / logout** — `POST /logout` bumps the user's
  `token_version` (embedded in the JWT), invalidating all previously issued tokens.
- **Non-atomic multi-write operations** — the multi-step flows now run as a unit
  of work: their repositories only stage (`flush`) and the service commits once,
  so the operation persists fully or not at all. `register` consumes the invite +
  creates the user atomically; `create_team` creates the team and both admin
  memberships in a single transaction (the platform admin is the team's first
  admin, plus the named lead). Single-write repositories still commit per call.
