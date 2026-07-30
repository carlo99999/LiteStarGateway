# MCP tool servers

The gateway can hold a registry of **MCP tool servers** — remote endpoints that
advertise tools a model could call — and govern who may use them.

!!! warning "Registry and inventory only, so far"
    This page describes what ships today: registering servers, discovering what
    they offer, classifying tools, and restricting them per API key. The gateway
    does **not** yet execute tool calls, and no model request behaves differently
    because a server is registered. Declaration injection and bounded execution
    are separate, later slices of
    [the design](next-steps/mcp-tool-gateway.md).

## 1. Authorize where the gateway may connect (required)

A tool server is a **team** resource — its admins register and remove it — but
the platform keeps one veto over where any of them may point:

```bash
MCP_ALLOWED_HOSTS=tools.internal:8443,10.9.0.0/24
```

Empty is the default, and it refuses everything. A deployment that upgrades
gains no new egress reach until an operator opts in, so no team can register a
server until this is set.

The grammar is the same as
[`OPENAI_COMPATIBLE_ALLOWED_HOSTS`](self-hosted-models.md#1-authorize-the-endpoint-required)
— `<target>[:<port>]`, where the target is a hostname, an IP literal, or a CIDR
block — and so is the distinction between the two kinds of entry. It matters more
here, so it is worth restating in the terms of this feature:

| entry | what it authorizes | refuses a DNS rebind? |
|---|---|---|
| `tools.internal:8443` | that **name** | **no** |
| `10.9.0.0/24:8443` | those **addresses** | yes |

The gateway re-resolves a server's host on **every** discovery and every call, not
only when the server was registered. With a CIDR entry that re-check is a
rebinding defence: a name that resolved into the allowlisted range yesterday and
resolves elsewhere now is refused. With a **name** entry it is not — you are
vouching for that name's DNS, by design. Re-resolving still matters for a name
entry, because it is what makes *removing* an entry take effect on servers that
are already registered.

If you are pointing at infrastructure you control, prefer the CIDR form.

The URL must also be `https`, and must not carry userinfo — the
`name:secret@host` form. The endpoint is kept in the clear for logs and metric
labels, so a password embedded in the URL would be logged verbatim. Use the bearer
token instead, which is envelope-encrypted and never returned by any endpoint.

## 2. Who may do what

| | `tools:propose` | `tools:read` | `tools:manage` |
|---|---|---|---|
| team **admin** | ✅ | ✅ | ✅ |
| member · model-manager · key-issuer · billing-viewer | ✅ | ❌ | ❌ |
| platform admin | ✅ | bypasses | bypasses |
| platform auditor | ❌ | ❌ | ❌ |

`tools:manage` is withheld from `model-manager` on the guardrail precedent:
attaching a tool server is an egress decision, not a model one. The platform
auditor's cross-team grant is strictly read-only *billing* visibility, and a tool
inventory is not billing.

`tools:propose` is held by every team role including `member`, so anyone can ask
for a server to be registered. A proposal changes no policy until an admin
approves it — see [§8](#8-proposals-a-member-asks-an-admin-decides).

## 3. Discovery is explicit, and it is a manage-level action

Registering a server does **not** contact it. Asking what it offers is a separate
action, because it makes the gateway open a connection to an operator-supplied
endpoint:

```http
POST /teams/{team_id}/mcp-servers/{server_id}/discover
```

It sits under `tools:manage` for that reason, and it is audited — the outbound
request was made on somebody's behalf, and that is what an operator later needs
attributed.

The result is cached. Within `MCP_INVENTORY_TTL_SECONDS` (default `3600`) a
refresh answers from storage without contacting the server, so a console left
open does not turn every visit into traffic to somebody's endpoint. Pass
`?force=true` when you know the server changed.

Three outcomes are worth telling apart, and the console shows them differently:

- **discovery has never run** — the inventory is empty because nobody asked;
- **the server advertises no tools** — it was asked, and offers nothing;
- **discovery failed** — a `502`, with the reason. The inventory you are looking
  at is whatever was stored before, not a live answer.

A malformed `tools/list` is the third case, never a shorter list: a
partially-parsed inventory would hide the fact that entries were dropped.

## 4. Effects are declared, never detected

Every tool carries an **effect** an operator sets:

| effect | meaning |
|---|---|
| `read` | reads only |
| `write` | changes something |
| `destructive` | deletes or irreversibly changes something |

MCP lets a server describe its own tools with hints (`readOnlyHint`,
`destructiveHint`). The gateway uses them to **seed** a tool nobody has
classified yet, and never to overwrite a classification. So re-running discovery
is safe: a server cannot downgrade a tool it previously advertised as destructive
by editing its own hints later.

**A tool nobody classified counts as `destructive`.** That is the safe end of the
default, and it has a consequence for §5.

Only the team that *owns* a server may set effects. A team that merely sees a
global server cannot relabel a destructive tool as harmless for everybody else.

## 5. Per-key policy

By default a key may invoke every tool **except** those declared `destructive` —
the same polarity a missing spend cap already has, so the feature works as soon
as a server is registered.

```http
PUT /teams/{team_id}/keys/{key_id}/tool-policy
{"allowed_tools": ["search"], "destructive_enabled": false}
```

- an **empty** `allowed_tools` means every tool, not none. The row exists mainly
  to carry the switch below, and an operator enabling destructive tools should not
  have to enumerate every read tool to keep them working;
- `destructive_enabled` is off unless set. Because an unclassified tool counts as
  destructive, turning it on also permits every tool on that server that nobody
  has reviewed yet — which is the reason to classify before enabling;
- removing the policy makes the key permissive again. It is a **widening**, not a
  cleanup, and it is audited as one.

Both the read and the write live under `tools:read`/`tools:manage`, deliberately
not under `keys:issue`. An earlier version of per-key *spend caps* had the write
in one permission domain and the read in another, so a key issuer could save an
object and then be refused the read of it.

## 6. Removing a server: two different verbs

`DELETE /teams/{team_id}/mcp-servers/{server_id}` does one of two things, and the
response says which:

| the server is | outcome | effect |
|---|---|---|
| this team's own | `deleted` | the server and its inventory are gone |
| global or extended to this team | `detached` | hidden from **this team only**; every other team keeps it |

A team admin cannot delete a shared server, only stop using it. A detach is
reversible with `POST .../{server_id}/reattach`, which the console offers by id —
a detached server is by definition absent from the team's list.

Deleting a shared server for everyone is a platform-admin action, under
`/platform/mcp-servers`.

## 7. Sharing a server across teams

A platform admin can promote a team's server to **global** (visible to every
team) or **extend** it to chosen teams (a grant, not a copy — the source stays
the single source of truth for its endpoint and token).

Both refuse a **name collision**. A server is referenced by its name and has no
alias, so a global `github` beside a team's own `github` would put two servers
under one name in front of that team. Rename one and retry; the error names both
sides.

## 8. Proposals: a member asks, an admin decides

Requiring `tools:manage` for every registration puts the person who knows which
tool the application needs behind the person who holds the permission. So any team
member may file a **proposal**, and a team admin (or a platform admin) decides.

```http
POST   /teams/{team_id}/mcp-server-proposals            # tools:propose
GET    /teams/{team_id}/mcp-server-proposals            # tools:propose
GET    /teams/{team_id}/mcp-server-proposals?pending=true
POST   /teams/{team_id}/mcp-server-proposals/{id}/approve   # tools:manage
POST   /teams/{team_id}/mcp-server-proposals/{id}/reject    # tools:manage
```

A proposal carries the same fields as a server, including the optional bearer
token, which is encrypted on write and never returned — the approver sees the
name, the url and the requested tools, never the secret. There is no *edit*: an
approver takes it as filed or refuses it with a reason. An admin who wants
different settings registers the server directly.

Four behaviours are worth knowing before you rely on this.

**Filing contacts nobody.** The url is validated offline — scheme, no userinfo,
and membership of `MCP_ALLOWED_HOSTS`. The first `tools/list` runs at *approval*.
Otherwise the lowest privilege in the system would hold a primitive for making the
gateway connect somewhere, even inside the allowlist.

**Approval re-checks the allowlist.** A host that was allowlisted when the
proposal was filed and has since been removed is refused at approval with a `400`,
and the proposal stays `pending`. Either re-add the entry and approve again, or
refuse it with a reason. Nothing about a pending row reserves a host.

**A rejection requires a reason**, and the person who filed it can read it: the
queue is readable by every team role, which is why "it disappeared" is never the
answer. A blank reason is a `400`.

**A second decision is a `409`.** Two admins approving the same proposal at the
same moment produce **one** server — the decision is claimed with a single
conditional update, and the loser is told somebody got there first rather than
creating a duplicate.

If approval succeeds but the tool server is unreachable, the server is still
registered: the approval is settled, and the inventory simply reads "no discovery
has run yet". Run a discovery once the server is up.

Both outcomes are audited (`mcp_server_proposal.approve` /
`mcp_server_proposal.reject`), as is the filing.

In the console, the **Tools** page carries the queue at the bottom. A member sees
that page for the queue alone — the server registry above it stays admin-only.
