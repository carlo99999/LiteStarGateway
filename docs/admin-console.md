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

A model is an alias (what clients call) mapped to a provider model id and a
credential. The credential picker is filtered to the chosen provider — the
backend rejects a mismatch. Optional per-token input/output costs feed usage
accounting and routing-savings math; an optional `max_output_tokens` caps each
call. Models can be **edited** (upstream id, costs, ceiling, Azure api version;
provider and credential are immutable — recreate to change them) and disabled
without deleting them.

**Default costs.** When you enter a known upstream model id (e.g. `gpt-4o`),
the input/output costs prefill from a bundled snapshot of the community price
map — edit as needed. Unknown ids leave the fields blank. The lookup is served
locally (`GET /model-prices`), so it works air-gapped.

#### Scope: team, global, and extended

A model belongs to a **team**, or to the **platform** (a *global* model). The
Models page has a global section (platform-admin) and a per-team section.

- **Global** models are callable by **every team**, present and future. Create
  one with *Add global model*, or promote a team model with *make global*.
- **Extend** shares a team-owned model with chosen teams — a *grant*, not a
  copy: the target team calls the **same** model (one source of truth, so cost
  and config edits propagate; disabling it affects everyone). Use the *extend*
  action to pick teams or revoke.

Each team's model list shows everything it can call, tagged by origin: `team`
(its own — editable here), `extended · <source team>`, and `global · from
<team>` (or just `global`). `GET /v1/models` lists the same set by effective
alias, so clients can discover them.

**Alias clashes never hide a model** — they rename. Extending `gpt-4o` to a
team that already has its own `gpt-4o` exposes the extended one as
`gpt-4o-<source team>`; a global shadowed by a team's own model is reachable as
`gpt-4o-global`.

!!! tip "Configuration gotchas"
    - **`provider_model_id` must exist on that provider.** The gateway forwards
      it verbatim; a Vertex model pointed at `gpt-4o` gets a 404 *from Vertex*.
      Use the provider's own deployment/serving name.
    - **Reasoning models need token headroom.** A model that "thinks" (e.g.
      Gemini 2.5) spends output tokens on reasoning first — a tiny `max_tokens`
      can return empty content. Give it room.
    - Upstream rejections surface as a `4xx`/`5xx` with the provider's status,
      so a `400`/`404` on one model with others working points at that model's
      credential or id, not the gateway.

### API keys

Issued per team, shown globally or on the team page. Two kinds:

- **Personal** — attributed to your account, inference-only by design.
- **Service principal** — a team identity; the only kind that may hold
  `management` or `all` scope. The table's *owner* column tells them apart.

Keys support an optional **per-key rate limit** (requests/minute), an optional
**expiry** (TTL in days — the key stops authenticating after it), and
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

### Playground

Team admins and model managers can compare up to five unique chat-model or
router aliases per request. These are real provider calls: each selected alias
consumes the team's RPM and budget and creates its own usage record and trace.
The batch is also audit-logged against the signed-in user. External router
strategies (webhook, judge, embeddings, and hybrid escalation) are not executed
during preview; the preview uses the router's default capable model so it cannot
create a hidden, unmetered side effect.

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
