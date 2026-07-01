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
`JWT_SECRET` (login token signing), `SALT_KEY` (credential encryption at rest),
and `ENVIRONMENT` (`development` default; `production` enables startup config
checks — a missing/default `JWT_SECRET` then aborts startup).

```bash
uv run litestar --app litestar_test.app:app run
uv run pytest
```

## Roadmap

Planned work, split into **Toward v1** (must-have before publishing) and **v2**
(after v1). Each item with a design doc has it parked on its own branch (linked) —
we resume from there. Order within a phase is a recommendation; reorder as needed.

### Toward v1 (pre-launch)

1. ✅ **CI (GitHub Actions)** _(shipped)_ — runs `ruff` + `pyrefly` + `pytest` on
   every push to `main` and every PR ([`.github/workflows/ci.yml`](.github/workflows/ci.yml),
   [design](docs/ci.md)).
2. **Container image & deployment** — Dockerfile, uvicorn workers + `--proxy-headers`, TLS, deploy guide.
   [`adding-docker-deploy`](https://github.com/carlo99999/LiteStarGateway/blob/adding-docker-deploy/docs/deployment.md)
3. **Database migrations (Alembic)** — replace `create_all` with versioned migrations.
   [`adding-db-migrations`](https://github.com/carlo99999/LiteStarGateway/blob/adding-db-migrations/docs/db-migrations.md)
4. **Production Postgres** — asyncpg + connection pool + validate concurrency on PG.
   [`adding-postgres`](https://github.com/carlo99999/LiteStarGateway/blob/adding-postgres/docs/postgres.md)
5. **Provider resilience** — timeouts, bounded retries with backoff, per-provider circuit breaker.
   [`adding-provider-resilience`](https://github.com/carlo99999/LiteStarGateway/blob/adding-provider-resilience/docs/provider-resilience.md)
6. **Request parameter allowlist** — deny-by-default sanitizing of client params before the provider SDKs.
   [`adding-param-allowlist`](https://github.com/carlo99999/LiteStarGateway/blob/adding-param-allowlist/docs/param-allowlist.md)
7. **Structured logging & error hygiene** — JSON logs, request ids, no internal leakage on 5xx.
   [`adding-structured-logging`](https://github.com/carlo99999/LiteStarGateway/blob/adding-structured-logging/docs/logging.md)
8. **Secrets management & key rotation** — supply secrets from a manager; rotate `SALT_KEY` (keyring + re-encrypt) and `JWT_SECRET`.
   [`adding-secrets-rotation`](https://github.com/carlo99999/LiteStarGateway/blob/adding-secrets-rotation/docs/secrets-rotation.md)
9. **Observability via MLflow** — `TraceSink` port + MLflow adapter (OSS or Databricks), off the hot path.
   [`adding-observability-via-mlflow`](https://github.com/carlo99999/LiteStarGateway/blob/adding-observability-via-mlflow/docs/observability.md)
10. **Usage & cost accounting + budgets** — authoritative usage records, `GET /usage`, pre-call budget enforcement.
    [`adding-usage-cost`](https://github.com/carlo99999/LiteStarGateway/blob/adding-usage-cost/docs/usage-cost.md)
11. **Account recovery & password change** — `POST /me/password` + admin reset (email flow optional).
    [`adding-account-recovery`](https://github.com/carlo99999/LiteStarGateway/blob/adding-account-recovery/docs/account-recovery.md)

### Enterprise (post-v1)

- **SSO / SCIM / RBAC / audit** — federate identity via OIDC/SAML (identity as a
  swappable `IdentityProvider` port), auto-provision via SCIM, map IdP groups to
  teams/roles, extend RBAC, and add an append-only audit log. Reuses the existing
  multi-tenant model and JWT session. _The path to being a governed, enterprise-
  grade gateway rather than a broad LiteLLM clone._
  [`adding-enterprise-sso`](https://github.com/carlo99999/LiteStarGateway/blob/adding-enterprise-sso/docs/enterprise-sso.md)

### v2 (after v1)

- **AWS Bedrock provider** — Converse API + boto3 (no hand-rolled SigV4), responses emulated.
  [`adding-bedrock`](https://github.com/carlo99999/LiteStarGateway/blob/adding-bedrock/docs/bedrock.md)
- **Weighted multi-model routing** — an alias splitting traffic across ≤5 models by percentage.
  [`adding-weighted-routing`](https://github.com/carlo99999/LiteStarGateway/blob/adding-weighted-routing/docs/weighted-routing.md)
- **Smart (judge-based) routing** — four difficulty tiers + a swappable judge adapter.
  [`adding-smart-routing`](https://github.com/carlo99999/LiteStarGateway/blob/adding-smart-routing/docs/smart-routing.md)
- **Web UI** — SPA over the JSON API for login + admin + usage dashboards.
  [`adding-web-ui`](https://github.com/carlo99999/LiteStarGateway/blob/adding-web-ui/docs/web-ui.md)
- **LICENSE & repo hygiene** — add a LICENSE, `CONTRIBUTING`, and a security policy _(no branch yet)_.
- **Test coverage gate** — enforce 80% (`--cov-fail-under`) in CI _(no branch yet)_.
- **Minor hardening** — `GET /v1/models`, request body-size limits, security headers, DB backups, dependency scanning (pip-audit / Dependabot), admin audit log, API-key expiry _(no branch yet)_.

## Security — known issues & follow-ups

Tracked items not yet implemented (see also the code review notes):

- **Unvalidated request passthrough** — chat/responses requests are forwarded to
  the provider SDKs largely as-is (`{**model.params, **request}`). `model` is
  overridden, but other fields (e.g. `extra_headers`, large `n`) pass through.
  Consider an allowlist of accepted parameters per operation.
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
- **Email enumeration on signup** — a duplicate email now returns the same
  generic `400` as other client errors (the email is never echoed), so the
  response no longer reveals whether an address is registered. Because signup is
  invite-gated and the invite is consumed *before* the email check, probing an
  address costs one single-use, admin-issued invite per attempt — so enumeration
  is bounded by invite scarcity, not just by the (now generic) response.
- **Rate limiting** — `/v1/*` is throttled **per API key** (hashed, never the
  plaintext; falls back to per-IP for anonymous/invalid tokens) to bound provider
  spend, and `/login` + `/signup` are throttled **per IP** to bound brute force
  and account spam. Limits are conservative constants in
  `infrastructure/web/rate_limit.py`; back the store with Redis for multi-process
  deploys, and set the real client IP upstream (e.g. `--proxy-headers`) behind a
  proxy, since `X-Forwarded-For` is not trusted by default.
- **Non-atomic multi-write operations** — the multi-step flows now run as a unit
  of work: their repositories only stage (`flush`) and the service commits once,
  so the operation persists fully or not at all. `register` consumes the invite +
  creates the user atomically; `create_team` creates the team and both admin
  memberships in a single transaction (the platform admin is the team's first
  admin, plus the named lead). Single-write repositories still commit per call.
- **`JWT_SECRET` dev default** — with `ENVIRONMENT=production` (or `prod`) the app
  fails fast at startup if `JWT_SECRET` is unset or left at the insecure dev
  default, so a misconfigured production deploy can't sign tokens with a known
  key. Outside production the dev default is still allowed for convenience.
