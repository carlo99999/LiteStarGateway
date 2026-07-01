# Design doc — Enterprise: SSO, SCIM, RBAC, audit

> **Status:** Draft / parked (Enterprise, post-v1). Branch `adding-enterprise-sso`.
> No code yet.

## 1. Goal

Make the gateway adoptable by enterprises: federate identity via **SSO
(OIDC/SAML)**, provision users automatically via **SCIM**, map IdP groups to our
**teams/roles**, extend **RBAC** to fine-grained permissions, and record an
**audit log** of privileged actions. The multi-tenant foundation (users, orgs →
teams → memberships, roles, JWT sessions) already exists — this federates and
governs it, it does not replace it.

## 2. SSO — identity as a port

The clean fit for the hexagon: authentication becomes a swappable adapter.

```
domain/ports.py        IdentityProvider (Protocol):
                         authorize_url(state) -> str
                         exchange(callback_params) -> ExternalIdentity
domain/entities.py     ExternalIdentity (issuer, subject, email, groups, attrs)
infrastructure/web/session/sso/
    oidc_adapter.py     OIDC/OAuth2 (authlib): discovery, PKCE, JWKS validation
    saml_adapter.py     SAML 2.0 (python3-saml): ACS, metadata, signature checks
application/sso_service.py   maps ExternalIdentity -> local User (+ teams), then
                             issues our existing login JWT
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

## 4. Group → team / role mapping

- A per-org **mapping**: IdP group/claim → `(team, role)`. On each SSO login (and
  on SCIM sync), reconcile the user's memberships to match their current IdP
  groups (add/remove/keep), so access follows the IdP as source of truth.
- Keep a manual-override escape hatch for platform admins.

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
