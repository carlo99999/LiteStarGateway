# Design doc — Secrets management & key rotation

> **Status:** Draft / parked (pre-v1). Branch `adding-secrets-rotation`.
> No code yet.

## 1. Goal

Define how the three secrets are supplied in production and, critically, **how to
rotate them** — especially `SALT_KEY`, which today cannot be changed without
making every stored credential undecryptable.

Secrets: `MASTER_KEY` (bootstrap admin password), `JWT_SECRET` (login token
signing), `SALT_KEY` (credential encryption at rest).

## 2. Supply in production

- All via environment / a secret manager (Vault, AWS/GCP secret manager, k8s
  secrets) — never in the image or repo. Document the expected source.
- `ENVIRONMENT=production` already fails fast on a missing/default `JWT_SECRET`;
  document the same expectation for `SALT_KEY` (credential ops already 503 if
  unset).

## 3. Rotation — the hard part

### 3a. `SALT_KEY` (credential encryption)

Rotating the key breaks decryption of existing ciphertext. Options:

- **Keyed/versioned encryption (recommended)**: prefix each ciphertext with a key
  id; support a small keyring `{kid: key}`. New writes use the current key; reads
  pick the key by kid. Rotation = add a new current key, then a **re-encrypt
  migration** that rewrites old rows to the new key, then retire the old key.
- The cipher (`infrastructure/crypto.py`) grows a keyring + kid prefix; a
  management command re-encrypts `credential.encrypted_values`.

### 3b. `JWT_SECRET`

- Support **multiple verification keys** (current + previous) so rotation doesn't
  invalidate all live tokens at once: sign with current, accept current+previous
  during a grace window. (Or accept the hard cutover — bumping the secret logs
  everyone out, which `logout`/`token_version` semantics already tolerate.)

### 3c. `MASTER_KEY`

- Only used at bootstrap; "rotation" = the admin changing their password. Add an
  admin **change-password** path so the account isn't pinned to the master key
  (ties to `adding-account-recovery`).

## 4. Open decisions

1. **Keyring format** for `SALT_KEY` (kid prefix scheme) and where kids live.
2. **JWT** graceful (current+previous) vs hard cutover on rotation.
3. **Re-encrypt tooling**: a CLI/management command vs an Alembic data migration
   (depends on `adding-db-migrations`).
4. **Secret backend**: which manager to document as the reference.

## 5. Testing

- Cipher keyring: encrypt with kid A, rotate, decrypt still works; re-encrypt
  rewrites to kid B; old key retired → old ciphertext no longer required.
- JWT: token signed with previous key still verifies during grace; rejected after.

## 6. Rollout

1. `feat/salt-keyring` — versioned cipher (kid) + re-encrypt command.
2. `feat/jwt-key-rotation` — current+previous verification keys.
3. `feat/admin-change-password` — decouple admin from `MASTER_KEY`.
