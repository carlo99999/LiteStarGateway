# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report them privately via
[GitHub private vulnerability reporting](https://github.com/carlo99999/LiteStarGateway/security/advisories/new)
(Security tab → *Report a vulnerability*). If you can't use GitHub, email the
maintainer listed in [`pyproject.toml`](pyproject.toml).

Please include what you'd need to triage it yourself: affected
endpoint/component, reproduction steps or a proof of concept, and the impact
you believe it has.

This is a single-maintainer project — you'll get an acknowledgment within
**5 business days** and a fix or a mitigation plan for confirmed issues as
fast as severity warrants. Coordinated disclosure: please give the fix a
chance to land and be released before publishing details.

## Scope

Reports we especially care about:

- Authentication/authorization bypass (JWT, API keys, scopes, service
  principals, SSO/OIDC)
- Cross-tenant access — reading or acting on another team's/org's models,
  keys, usage, or budgets
- Provider-credential exposure (encrypted at rest; they must never leave the
  server)
- Billing/budget bypass — consuming inference that isn't metered or gated
- Injection of any kind, SSRF, secret leakage in logs/errors/traces

Out of scope:

- Deployments that ignore the documented hardening (e.g. default secrets
  outside `development`, missing TLS/reverse proxy, `FORWARDED_ALLOW_IPS`
  opened to untrusted peers) — see
  [README → Deployment](README.md#deployment)
- Denial of service via resource exhaustion on unprotected self-hosted
  instances
- Vulnerabilities in upstream dependencies without a demonstrated impact on
  the gateway (dependencies are scanned with `pip-audit` in CI)

## Supported versions

| Version | Supported |
|---|---|
| latest `main` / most recent release | ✅ |
| older releases | ❌ — fixes land on `main` only |

## Known accepted limitations

Deliberate design decisions and tracked follow-ups are documented in
[README → Security](README.md#security--known-issues--follow-ups) and in the
code-review log ([`issues/INDEX.md`](issues/INDEX.md)) — please read those before
reporting, as some behaviors (e.g. platform-global provider credentials) are
intentional.
