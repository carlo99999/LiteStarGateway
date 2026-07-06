# Design doc — Account recovery & password change

> **Status:** Draft / parked (pre-v1). Branch `adding-account-recovery`.
> No code yet.

## 1. Goal

Users have no way to change or recover a password today. Before real users exist,
add at least: **self-service password change** (authenticated) and an
**admin-driven reset** (no email infra required). A full email-based
"forgot password" flow is optional and gated on having email delivery.

## 2. Design

### 2a. Change password (authenticated) — minimum

- `POST /me/password` (JWT-protected): verify current password, validate the new
  one (reuse the existing complexity check), store the new hash, and **bump
  `token_version`** so all other sessions are invalidated (logout-everywhere).
- Also lets the bootstrap admin move off `MASTER_KEY` (ties to
  `adding-secrets-rotation`).

### 2b. Admin reset — no email needed

- Platform-admin endpoint to reset a user's password: either set a temporary
  password returned once, or issue a single-use **reset token** (like invites)
  the user redeems to set a new password. Bump `token_version` on reset.

### 2c. Email-based self-service reset (optional, needs email)

- `POST /password-reset/request` (per-IP rate-limited, **non-revealing** like
  signup — always 202 regardless of whether the email exists) → emails a
  single-use, expiring token.
- `POST /password-reset/confirm` → validates token, sets new password, bumps
  `token_version`.
- Requires an **email adapter** (a port + SMTP/provider adapter) — the first real
  need for outbound email in the system.

## 3. Placement

```text
application/user_service.py   change_password, admin_reset, (reset_request/confirm)
domain/entities.py            PasswordReset (single-use, expiring) if token-based
domain/ports.py               EmailSender (only for 2c)
infrastructure/web/...        /me/password, admin reset, (password-reset/*)
```

Reuse existing pieces: password complexity check, `token_version` revocation,
the single-use-token pattern from invites, per-IP rate limiting, and the
non-revealing response pattern from signup.

## 4. Open decisions

1. **Scope for v1**: change-password + admin-reset (no email) — recommended
   minimum. Email-based reset deferred until an email adapter exists.
2. **Admin reset shape**: temp password vs reset token.
3. **Email provider** (2c): SMTP vs a transactional provider; introduces the
   first `EmailSender` port.
4. **Token expiry** for reset tokens.

## 5. Testing

- Change password: wrong current → 401; weak new → WeakPassword; success bumps
  `token_version` (old JWT rejected, matching the logout tests).
- Admin reset: non-admin forbidden; reset invalidates old sessions.
- (2c) request is non-revealing + rate-limited; confirm is single-use + expiring.

## 6. Rollout

1. `feat/change-password` — `POST /me/password` (+ admin off MASTER_KEY).
2. `feat/admin-password-reset` — admin-driven reset.
3. *(optional, needs email)* `feat/email-password-reset` — `EmailSender` port +
   request/confirm endpoints.
