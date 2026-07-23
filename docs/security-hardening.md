# Security — model, known limits, and hardening history

How to report a vulnerability: see [SECURITY.md](../SECURITY.md). This page
documents the security *model*, the accepted limits, and the hardening work
already shipped (moved here from the README as the list grew).

## Security model in brief

- **Secrets**: provider credentials are encrypted at rest with a rotating
  keyring (envelope encryption; `SALT_KEY` is the master). They are write-only —
  no endpoint ever returns them. JWT signing keys rotate the same way under
  `JWT_SECRET`.
- **Identity**: invite-only signup (team + role required), per-account login
  lockout with exponential backoff, admin-issued password resets, optional
  OIDC SSO + SCIM provisioning. Browser sessions are HttpOnly cookies with
  CSRF protection.
- **Authorization**: declarative role → permission mapping, centrally enforced.
  Personal API keys are inference-only by design; management scope requires a
  service principal. Keys support optional expiry (TTL) and grace-window
  rotation. Platform admins are gateway-governed (not deactivatable via SCIM).
- **Spend safety**: per-team budgets enforced pre-call (402), per-team and
  per-key rate limits, request parameter allowlist with cost-driver clamping,
  a configurable request body-size cap (`MAX_BODY_SIZE`), and in-flight spend
  reservation against burst overshoot.
- **Transport**: static security response headers on every response
  (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, and HSTS when behind TLS). No CSP is emitted by the app —
  a correct policy for the built SPA belongs at the reverse proxy.
- **Attribution**: append-only audit trail for privileged actions; usage
  accounting survives transient DB failures via a durable outbox.

## Known limits (accepted, tracked)

- **Cross-team credential usage (by design)** — credentials are
  platform-global, so any team admin can reference any credential in a model
  and consume it (they cannot read its secret). This is intentional for now;
  tie credentials to a team/org if per-team isolation becomes a requirement.
- **SQLite is the dev/test default** — the zero-config default is file SQLite
  (single-writer, weak concurrency); the test suite runs on it. Production
  **requires** the Postgres backend (the compose default) — the app fails fast
  at startup on SQLite there. A Postgres job in CI covers the migration chain
  and runs the full suite against the production dialect.
- **No branch protection / CI merge gate (single maintainer)** — `main` is not
  protected and there is no required CI check on merge. This is an accepted
  limit while the project has a single contributor: every change is still
  developed on a branch, opened as a PR, and merged only after the local gate
  (`pytest`, `pyrefly`, `pre-commit`) is green. Before adding other
  contributors, enable branch protection on `main` requiring PR review + a
  green CI run.

## Hardening shipped

- **Durable billing** — a failed usage-ledger write no longer just
  logs-and-drops: the event is dead-lettered to a `pending_usage_event` outbox
  and a background reconciler (every 60s) retries it into `usage_event`
  (idempotent by event id). The synchronous ledger write on success is
  unchanged, so `/usage` stays immediately consistent. *Caveat: the outbox is
  in the same Postgres, so a total DB outage still can't be survived — it
  recovers transient/contention failures and provides at-least-once capture;
  failed dead-letter writes fall back to an ERROR log with the full event.*
- **Audit log** — privileged actions are recorded to an append-only
  `audit_event` table (who / what / target / from where / when) and read via
  `GET /audit` (platform-admin, paginated, newest first). Written synchronously
  and durably off the inference hot path.
- **Unvalidated request passthrough** — the client's OpenAI-shaped body is
  sanitized against a per-operation allowlist before it reaches the provider
  SDK (`domain/request_policy.py`), so SDK-special kwargs (`extra_headers`,
  `extra_body`, `extra_query`, `timeout`, …) are dropped and cost drivers
  (`n`, `max_tokens`) are clamped. Trusted `model.params` are merged
  separately.
- **Credential exfiltration via model `api_base` (SSRF)** — the provider
  endpoint comes only from the admin-managed credential, never from the
  team-controlled model (`Model.api_base` was removed). A team admin cannot
  redirect a credential's secret to an arbitrary host. The routing webhook
  URL is likewise refused when it targets private/loopback/link-local
  addresses.
- **Invite single-use race (TOCTOU)** — invites are consumed with an atomic
  conditional `UPDATE … WHERE used_at IS NULL`, so concurrent signups can't
  reuse one invite. Invite tokens are kept out of request URLs server-side.
- **Login lockout** — after repeated failures, password logins lock with an
  exponentially escalating window (capped), indistinguishable from a plain
  wrong password (no enumeration/lock oracle). Admins can lift a lock.
- **No token revocation / logout** — `POST /logout` bumps the user's
  `token_version` (embedded in the JWT), invalidating previously issued
  tokens. Browser sessions moved to HttpOnly cookies + CSRF.
- **Email enumeration on signup** — a duplicate email returns the same generic
  `400` as other client errors. Because signup is invite-gated and the invite
  is consumed *before* the email check, probing an address costs one
  single-use, admin-issued invite per attempt.
- **Rate limiting** — `/v1/*` is throttled **per API key** (hashed; falls back
  to per-IP for anonymous/invalid tokens), `/login` + `/signup` **per IP**.
  Optional per-team and per-key RPM limits gate admission on top. Back the
  store with Redis for multi-process deploys; inactive rate-limit buckets are
  pruned.
- **Atomic multi-write operations** — multi-step flows run as a unit of work:
  repositories stage (`flush`) and the service commits once. `register`
  consumes the invite + creates the user + adds the team membership
  atomically; `create_team` creates the team and both admin memberships in a
  single transaction.
- **`JWT_SECRET` dev default** — with `ENVIRONMENT=production` the app fails
  fast at startup if `JWT_SECRET` is unset or left at the insecure dev
  default. Outside production the dev default is allowed for convenience.
- **Key rotation with grace** — rotating an API key issues the replacement and
  expires the old key after a 1-hour grace window (time-aware `is_active`), so
  rotation never needs a downtime window.
- **`last_used_at` write throttling** — the API-key auth hot path persists
  `last_used_at` at most once per minute per key.
- **API-key expiry (TTL)** — keys can be issued with `expires_in_days`; a
  time-aware `is_active` stops them authenticating past `expires_at`, so
  short-lived credentials lapse on their own instead of relying on manual
  revocation. Rotation preserves the original's absolute expiry.
- **Request body-size cap** — bodies over `MAX_BODY_SIZE` (default 10 MB) are
  rejected before they're read; explicit and tunable per deployment.
- **Static security headers** — every response carries `nosniff`,
  `X-Frame-Options: DENY`, a `Referrer-Policy`, and HSTS when behind TLS.
- **Routing-decision data hygiene** — routing decisions are keyed by
  `router_id`, so a deleted router's history (including the JSONL distillation
  export's raw prompts) never surfaces under a later router that reused its
  name.
