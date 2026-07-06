# Design doc — Secrets management & key rotation

> **Status:** Implemented — envelope-encryption keyring
> (`infrastructure/keyring.py`, `infrastructure/crypto.py`) with rotating data keys
> stored in the DB (`SecretKey`; migration `add_secret_key_keyring_credential_key_id`),
> and current+previous JWT verification keys for graceful rotation. Retained as the
> original design rationale.

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

Rotating the key breaks decryption of existing ciphertext. Implemented approach:

- **Envelope encryption with a DB keyring**: a fixed **master key derived from
  `SALT_KEY`** (never auto-rotated) wraps rotating **data keys** stored in the DB
  (`SecretKey` rows, `KeyPurpose.CREDENTIAL`). Credential values are encrypted with
  a data key, and each credential row records **which key encrypted it** via a
  `credential_key_id` column (not a kid prefix on the ciphertext), so existing rows
  stay readable across rotations. New writes use the active data key; reads select
  the key by the row's `credential_key_id`.
- Rotation = add a fresh active data key (`Keyring.new_credential_key`), re-encrypt
  old rows to it, then retire the superseded keys
  (`Keyring.retire_old_credential_keys`); retired keys stay readable until nothing
  references them. `crypto.py` holds `MasterCipher`/`DataCipher`; `keyring.py` holds
  the keyring operations.

### 3b. `JWT_SECRET`

- Implemented: **multiple verification keys** (current + previous) so rotation
  doesn't invalidate all live tokens at once — sign with the active key, accept all
  still-usable keys during a grace window (`Keyring.active_jwt_secret` /
  `jwt_verification_secrets` / `rotate_jwt`, which keeps a retention grace beyond
  the token TTL before deleting old keys).

### 3c. `MASTER_KEY`

- Only used at bootstrap; "rotation" = the admin changing their password. Add an
  admin **change-password** path so the account isn't pinned to the master key
  (ties to `adding-account-recovery`).

## 4. Decisions (as implemented)

1. **Keyring format**: a DB keyring (`SecretKey` rows) of master-wrapped data keys;
   credentials reference the key via a `credential_key_id` column (no kid prefix on
   the ciphertext).
2. **JWT rotation**: graceful (current + previous verification keys), not a hard
   cutover.

Still open:

3. **Re-encrypt tooling**: a CLI/management command vs an Alembic data migration.
4. **Secret backend**: which manager to document as the reference.

## 5. Testing

- Cipher keyring: encrypt with kid A, rotate, decrypt still works; re-encrypt
  rewrites to kid B; old key retired → old ciphertext no longer required.
- JWT: token signed with previous key still verifies during grace; rejected after.

## 6. Rollout

1. `feat/salt-keyring` — versioned cipher (kid) + re-encrypt command.
2. `feat/jwt-key-rotation` — current+previous verification keys.
3. `feat/admin-change-password` — decouple admin from `MASTER_KEY`.
