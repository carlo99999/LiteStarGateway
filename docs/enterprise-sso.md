# Design doc — Enterprise: SSO, SCIM, RBAC, audit

> **Status:** OIDC SSO implemented (`infrastructure/sso/oidc.py`,
> `infrastructure/web/session/sso.py`) and the audit log is implemented
> (`infrastructure/persistence/audit_repository.py`, `infrastructure/web/audit/`,
> wired into invites / password-reset / unlock / set-active). SCIM, extended RBAC,
> and SAML remain parked. The rest of this doc is the broader enterprise plan.

## 0. OIDC SSO — implementation plan (refined after studying LiteLLM)

Design **inspired by LiteLLM's OIDC SSO** (`litellm/proxy/management_endpoints/ui_sso.py`,
MIT) — patterns ported, **no code copied**; nothing from LiteLLM's proprietary
`enterprise/` was used.

**Ported patterns:** select the provider by config; on callback map the identity
to a user; **derive role from IdP groups** via a hierarchy where the
highest-privilege match wins with a default fallback (their
`determine_role_from_groups`); **JIT-provision** the user on first login;
reconcile team membership from groups (their `add_missing_team_member`).

**Library — Authlib, not fastapi-sso.** LiteLLM uses `fastapi-sso` because it *is*
FastAPI. Two spike findings pushed us off it: (1) its `OpenID` result has **no
`groups`** (LiteLLM digs groups out of the raw JWT separately), and (2) it's
FastAPI/Starlette-`Request`-coupled — awkward under Litestar. **Authlib** is
framework-agnostic (httpx), installs on Python 3.14, and gives full `id_token`
claim access (incl. `groups`), OIDC discovery, PKCE, and JWKS verification.

**Flow / components (hexagonal):**

- **Port** `IdentityProvider` (`infrastructure/sso/oidc.py`,
  `OIDCIdentityProvider`):
  `authorization_url(state, redirect_uri, *, nonce, code_verifier) -> str`,
  `exchange(code, redirect_uri, *, nonce, code_verifier) -> ExternalIdentity(subject, email, email_verified, groups)`.
- **Adapter** `OIDCIdentityProvider` (Authlib + joserfc): discovery + PKCE (S256) +
  token exchange + `id_token` verification against the provider's JWKS. The
  **`nonce`** claim is verified on exchange (replay defense — an id_token minted
  for a different authorization request is rejected). The **`groups`** claim must
  be a JSON array or the exchange fails closed (a non-list value is not iterated).
  Discovery metadata is cached with a **TTL refresh** (~1h) and the JWKS is
  refreshed lazily on an unknown-`kid` miss, so IdP endpoint/key rotation is picked
  up without a restart. Generic OIDC, with Google/Microsoft as config presets.
- **Endpoints** (2): `GET /sso/login` (redirect to IdP, signed `state` + PKCE +
  `nonce`) and `GET /sso/callback` (exchange → map → JIT upsert → **issue our own
  JWT** via the existing keyring; rate-limited, non-revealing). The web layer lives
  in `infrastructure/web/session/sso.py`.
- **Pure mapping**: the platform-admin flag is derived from an admin-group set
  (`OIDC_ADMIN_GROUPS`), upgrade-only. SSO does **not** manage teams — team
  membership is managed inside the gateway (see §4).
- **JIT upsert** in `UserService`: create/update the SSO user (no password),
  apply the platform role, bump `token_version` if disabled. `User` gains
  `sso_subject`/`external_id` and `is_active`.
- **Config**: OIDC discovery URL, client id/secret, scopes, `OIDC_ADMIN_GROUPS`,
  `DEFAULT_ROLE` (§4). **`OIDC_REDIRECT_URI` is required when SSO is enabled
  outside local dev** (`config.py` fails fast if unset) — otherwise the redirect is
  derived from the request `Host` header, which a forged Host could steer.
- **Reuse**: the existing JWT session + keyring — SSO federates identity, then
  mints our token (no second session system).

**First PR scope:** generic OIDC (+ Google/Microsoft presets), login+callback, JIT
upsert, group→admin, tests with a fake `IdentityProvider`.
**Follow-ups:** SCIM (deprovisioning), SAML (python3-saml), audit log, per-org
SSO, fine-grained RBAC (§ below).

## 1. Goal

Make the gateway adoptable by enterprises: federate identity via **SSO
(OIDC/SAML)**, provision users automatically via **SCIM**, map IdP groups to our
**teams/roles**, extend **RBAC** to fine-grained permissions, and record an
**audit log** of privileged actions. The multi-tenant foundation (users, orgs →
teams → memberships, roles, JWT sessions) already exists — this federates and
governs it, it does not replace it.

## 2. SSO — identity as a port

The clean fit for the hexagon: authentication becomes a swappable adapter.

```text
domain/ports.py        IdentityProvider (Protocol):
                         authorization_url(state, redirect_uri, *, nonce, code_verifier) -> str
                         exchange(code, redirect_uri, *, nonce, code_verifier) -> ExternalIdentity
domain/entities.py     ExternalIdentity (subject, email, email_verified, groups)
infrastructure/sso/
    oidc.py             OIDCIdentityProvider (authlib + joserfc): discovery (TTL
                        refresh), PKCE, nonce, JWKS validation [shipped]
    saml_adapter.py     SAML 2.0 (python3-saml): ACS, metadata, signature checks [planned]
infrastructure/web/session/sso.py   login/callback endpoints
application/user_service.py   maps ExternalIdentity -> local User (+ teams), then
                              issues our existing login JWT (JIT upsert)
```

Flow: `/sso/{provider}/login` → IdP → `/sso/{provider}/callback` → validate →
**upsert** the local `User` (no password; SSO-only accounts) → issue the current
7-day JWT (or the refreshed model from the account-recovery doc). SSO **reuses the
existing session layer** — it only changes *how identity is established*.

Config: providers registered per deployment (or per organization for true
multi-tenant SSO) — client id/secret, discovery URL / SAML metadata, allowed
domains.

## 3. SCIM — provisioning / deprovisioning

- Implement the **SCIM 2.0** `/scim/v2/Users` (and `/Groups`) endpoints so the
  IdP can create/update/**deactivate** users automatically. Deactivation must
  disable login and bump `token_version` (revoke sessions).
- Auth for SCIM: a dedicated provisioning token (admin-issued), separate from user
  JWTs and `lsk_` keys.
- Maps onto existing `User`/`TeamMembership` tables; adds an `external_id` and an
  `is_active` flag to `User`.

## 4. Platform role & admin — how SSO maps to authorization — **implemented**

**Design principle: SSO authenticates, the gateway authorizes.** SSO answers *who
you are* (JIT-provisions your account, binds it to the IdP `sub`). *What you can
do* — teams, memberships, the platform-admin flag — lives in the gateway. There is
no IdP→team mapping to maintain: you create teams in the gateway and manage
membership there, exactly as for a password user.

The one authorization signal SSO carries is the **platform-admin flag**
(`User.is_admin`), which is binary: you are either a platform admin or a regular
member. (Team-level admin/member roles are separate and managed via the team API.)

### The three levers

| Lever | Where | What it does |
|-------|-------|--------------|
| `OIDC_ADMIN_GROUPS` | env, comma-separated | Membership in any listed IdP group **grants** platform admin at login. |
| `DEFAULT_ROLE` | env, `member` (default) / `admin` | Platform role a **brand-new** SSO account is created with at first login, when not matched by `OIDC_ADMIN_GROUPS`. |
| `PATCH /users/{id}/admin` | admin API | A platform admin **grants or revokes** another user's admin flag. The only way to **demote**. |

### The rule (upgrade-only)

On every SSO login the gateway computes the admin flag like this:

```text
group_admin   = (user's IdP groups) ∩ OIDC_ADMIN_GROUPS ≠ ∅
default_admin = (DEFAULT_ROLE == "admin")

brand-new account (JIT):   is_admin = group_admin OR default_admin
returning account:         is_admin = current is_admin OR group_admin   ← upgrade-only
```

**Upgrade-only** is the key property: groups and `DEFAULT_ROLE` only ever *grant*
admin. A re-login **never revokes** it — leaving the admin group does *not* demote
you, and a manual grant is never silently undone by the next login. Demotion is
done **only** through `PATCH /users/{id}/admin`. This is deliberate: it keeps the
IdP and the gateway from fighting over the same flag on every login.

The flag is read **live from the database on every request** (see
`provide_current_admin`) and is not carried in the JWT, so both a group-driven
grant and a manual promote/demote take effect on the user's *next* request — no
re-login or token refresh needed.

### How to configure it — common setups

- **Admins governed by the IdP (recommended for larger orgs).** Put the admin
  group in `OIDC_ADMIN_GROUPS` and add people to that group in your IdP; they
  become admins on their next login. Leave `DEFAULT_ROLE=member` so everyone else
  is a regular user. To remove an admin, remove them from the group **and** run
  `PATCH /users/{id}/admin {"is_admin": false}` (the group alone won't demote —
  upgrade-only).
- **Admins managed in the gateway.** Leave `OIDC_ADMIN_GROUPS` empty. New SSO users
  get `DEFAULT_ROLE` (usually `member`); promote/demote with the endpoint. SSO
  never touches the flag again.
- **Small trusted deployment.** `DEFAULT_ROLE=admin` — everyone who can SSO in is
  created as an admin.

### ⚠️ What `OIDC_ADMIN_GROUPS` must contain (Entra/Azure AD gotcha)

The gateway matches the values your IdP puts in the token's `groups` claim. **Entra
ID by default emits group *object IDs* (GUIDs), not display names.** So with the
default Entra config you list the GUID, not the name:

```env
OIDC_ADMIN_GROUPS=a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

To use the readable name instead (e.g. `GatewayAdmin`), configure the app
registration's group claim to emit group names (works best for groups synced from
on-prem AD). Okta/Keycloak/Google typically emit the name directly.

### Internals (where the logic lives)

- `config.py` — parses `DEFAULT_ROLE` (`_env_choice`, validated to `member|admin`,
  fails fast on a typo) and exposes `Settings.default_admin`.
- `infrastructure/web/session/sso.py` (`sso_callback`) — computes `group_admin`
  from the groups claim and calls the service with `group_admin` + `default_admin`.
- `application/user_service.py` (`upsert_sso_user`) — applies the upgrade-only rule
  above; `set_user_admin` implements the guarded promote/demote (admin-only, and an
  admin cannot change their **own** role — a self-lockout guard).
- `infrastructure/persistence/user_repository.py` — `bind_sso` writes the
  caller-computed flag; `set_admin` flips it for the manual endpoint.
- Audit: promote/demote emit `user.grant_admin` / `user.revoke_admin`.

Not yet done: per-org (rather than global) admin groups, and SCIM-driven
provisioning/deprovisioning (§3).

## 5. Extended RBAC

Today: platform-admin / team-admin / member. Enterprise needs finer control:

- Introduce a small **permission model** (roles → permissions), still enforced in
  the services (the API stays the source of truth; SSO/SCIM only populate
  identity). Keep it declarative and centrally checked, not scattered.
- Examples: "billing viewer", "model manager", "key issuer", read-only auditor.

## 6. Audit log

- Append-only record of privileged actions (login, SSO/SCIM events, key/credential
  create/revoke, team/member/role changes, budget changes): who, what, when, from
  where, result. Written off the hot path (like the trace sink / usage events).
- Exposed to platform/org admins; retained per policy.

## 7. Compliance (process, not just code)

Enterprise procurement asks for **SOC 2 / ISO 27001**, data residency, and a
retention/PII policy — especially relevant if payload logging (observability) is
on. These are mostly process + hosting, but the code must support: data export /
deletion, configurable retention, and region-pinned storage.

## 8. Open decisions

1. **OIDC-first vs SAML-first**: most SaaS IdPs do OIDC; large enterprises often
   require SAML. Lean: OIDC first (authlib), SAML second.
2. **SSO scope**: per-deployment single IdP vs **per-organization** IdP (true B2B
   multi-tenant). Per-org is the enterprise-grade answer but more work.
3. **Local + SSO coexistence**: keep password login for break-glass admin, or
   SSO-only? Recommend: SSO for users, a protected local admin for break-glass.
4. **JIT provisioning vs SCIM-only**: create users on first SSO login (JIT) and/or
   require SCIM. Support JIT, layer SCIM for deprovisioning.
5. **RBAC granularity**: how fine to go without over-engineering.

## 9. Testing

- OIDC/SAML adapters with faked IdP responses (JWKS/signature validation, PKCE,
  state); no real IdP in tests.
- SCIM: create/update/deactivate → memberships + `token_version` effects.
- Group mapping reconciliation (add/remove/keep) is a pure function → unit tests.
- Audit: privileged actions produce records; failures don't break the request.

## 10. Rollout (Enterprise phase)

1. `feat/oidc-sso` — `IdentityProvider` port + OIDC adapter + `sso_service` +
   JIT user upsert, reusing the JWT session.
2. `feat/sso-group-mapping` — IdP group → team/role reconciliation.
3. `feat/scim` — SCIM 2.0 Users/Groups + `external_id`/`is_active` on `User`.
4. `feat/rbac` — permission model + central enforcement.
5. `feat/audit-log` — append-only audit trail + admin read API.
6. `feat/saml-sso` — SAML adapter (if required by customers).
