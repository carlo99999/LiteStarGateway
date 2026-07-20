# Admin console

The gateway ships with a full admin console — a React SPA served by the gateway
itself under **`/ui`** (same origin as the API, no separate deployment). Every
management operation the API offers is available from the browser: tenancy,
users, models, credentials, API keys, smart routing, budgets, usage, and the
audit trail.

## Access & sessions

- Open `http://<host>/ui` and sign in with an admin account. On first boot the
  gateway bootstraps one platform admin from `ADMIN_EMAIL` + `MASTER_KEY`.
- Browser sessions use an **HttpOnly, same-origin cookie** (not a stored JWT):
  XSS cannot read the token. Unsafe requests carry a CSRF token; the console
  and API must share an origin (see the reverse-proxy notes in
  [operations](operations.md)).
- What you see is role-aware: platform admins get the whole platform, everyone
  else only the teams they belong to (see [Permissions](#permissions)).

## Dashboard

The landing page adapts to the signed-in role:

- **Platform admin** — organization/team/user counts, a gateway readiness
  indicator (polls `/health/ready`), platform-wide **smart-routing savings**,
  spend over the last 30 days with the top teams, and the latest audit events.
- **Everyone else** — the teams you belong to, with your role and each team's
  routing savings where your role grants `usage:read`.

## Governance

### Organizations

Top-level tenants. Create, rename, describe and tag them; each organization
page shows its teams and a **spend rollup** (last 30 days, per team). An
organization can only be deleted once it has no teams.

### Teams

Teams live inside an organization and own everything operational: models,
API keys, service principals, routers, budget, usage. The team page brings all
of those together in one place. Teams carry optional metadata
(description/tags) and an optional **per-team rate limit** (requests/minute,
enforced at request admission). Deleting a team is refused while it still has
models or API keys.

### Users & invites

Accounts are created **by invite**: pick the team the invitee joins and their
role there (both required — an account is never created team-less), and the
console hands you a **one-time signup link**. The invitee opens it, sets their
own password, and lands in the chosen team with the chosen role.

Per-user actions: grant/revoke **platform admin**, grant/revoke the read-only
**auditor** role, activate/deactivate (deactivation revokes sessions and
personal keys), lift a login lockout, and delete. Deletion is refused (409)
while the user still has team memberships or API keys they created — remove
those first, or deactivate instead.

### Service principals

Team-owned machine identities. A personal API key is deliberately
inference-only; **management-scoped keys belong to service principals**, so
automation credentials are never tied to a person's account. Create, disable
(its keys stop authenticating), or delete (all of its keys are revoked).

## Gateway plane

### Credentials

Platform-scoped provider secrets (OpenAI, Azure OpenAI, Anthropic, Vertex AI,
Bedrock, Databricks). The form shows exactly the fields each provider needs;
values are **write-only** — encrypted at rest with the rotating keyring and
never returned by any endpoint. Deleting a credential is refused while a model
still references it.

### Models

Per-team model deployments: an alias (what clients call) mapped to a provider
model id and a credential. The credential picker is filtered to the chosen
provider — the backend rejects a mismatch. Optional per-token input/output
costs feed usage accounting and routing-savings math; an optional
`max_output_tokens` caps each call. Models can be disabled without deleting
them.

### API keys

Issued per team, shown globally or on the team page. Two kinds:

- **Personal** — attributed to your account, inference-only by design.
- **Service principal** — a team identity; the only kind that may hold
  `management` or `all` scope. The table's *owner* column tells them apart.

Keys support an optional **per-key rate limit** (requests/minute) and
**rotation with a 1-hour grace window**: rotating issues a replacement (same
scope, owner, rate limit) and schedules the old key to expire an hour later,
so deployed clients can switch without downtime. Plaintext is shown exactly
once, at creation/rotation. Revoked keys stay visible in their own view.

### Routing (smart routers)

Virtual models: clients call the router's name and a strategy picks the best
candidate per request (hard capability filters run first; any strategy failure
falls back to the router's `default_model`). All six strategies are
configurable from the console:

| Strategy | What it does | Extra config |
|---|---|---|
| `complexity` | Rule-based prompt scoring into quality tiers | none |
| `weighted` | Traffic split proportional to candidate weights | weight per candidate |
| `judge` | A team chat model classifies the tier | judge model, char budget |
| `webhook` | Your HTTP endpoint picks the model | url, bearer token, timeout |
| `hybrid` | Rules first; only ambiguous prompts escalate | escalation judge/webhook |
| `embeddings` | Semantic routes matched by cosine similarity | embedding model + routes |

A **shadow strategy** can run alongside the live one, recording what it would
have chosen without affecting traffic. Each router's detail page shows the
decision distribution (by model / tier / shadow), the **estimated savings** vs
the most expensive capable candidate, the recent decision log, and a **JSONL
export** of judge decisions for distilling a local classifier.

## Observability

- **Usage** — per-model token and cost totals per team.
- **Budgets** — per-team spend caps (daily or monthly window) with live
  spent/remaining figures; enforced *before* every call (over budget ⇒ `402`).
  No budget means unlimited spend, and the page says so.
- **Audit** — the append-only trail of privileged actions: who did what, to
  what, from where. Every console mutation lands here.

## Permissions

| Role | Sees / does |
|---|---|
| **Platform admin** | Everything: tenancy, users, credentials, all teams, audit, platform savings. |
| **Platform auditor** | Read-only usage/spend across all teams + the audit log. |
| **Team admin** | Their team end-to-end: members, models, keys, routers, budget, usage. |
| `model-manager` / `key-issuer` / `billing-viewer` | One capability domain each within the team. |
| **Member** | Deliberately no permissions — exists to receive a personal inference key. |

Role → permission mapping is declarative and centrally enforced
(`domain/authorization.py`); the console renders 403s as "not visible" rather
than widening any permission.
