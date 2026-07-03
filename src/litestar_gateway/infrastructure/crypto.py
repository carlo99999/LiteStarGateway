"""Envelope encryption for secrets at rest.

A fixed **master key** (derived from `SALT_KEY`, never auto-rotated) wraps the
**data keys** stored in the DB keyring. Data keys rotate; the master does not.
Credential values are encrypted with a data key, and each ciphertext records
which key produced it, so existing data stays readable across rotations.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet

from litestar_test.domain.exceptions import SaltKeyMissing


def _derive_fernet_key(master_key: str) -> bytes:
    digest = hashlib.sha256(master_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def new_key_material() -> bytes:
    """Fresh random key material (a Fernet key; also usable as an HMAC secret)."""
    return Fernet.generate_key()


class MasterCipher:
    """Wraps/unwraps data-key material with the fixed master key."""

    def __init__(self, master_key: str) -> None:
        self._fernet = Fernet(_derive_fernet_key(master_key))

    def wrap(self, raw: bytes) -> str:
        return self._fernet.encrypt(raw).decode("utf-8")

    def unwrap(self, token: str) -> bytes:
        return self._fernet.decrypt(token.encode("utf-8"))


class DataCipher:
    """Encrypts/decrypts credential value dicts with a single data key."""

    def __init__(self, data_key: bytes) -> None:
        self._fernet = Fernet(data_key)

    def encrypt(self, values: dict[str, str]) -> str:
        return self._fernet.encrypt(json.dumps(values).encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> dict[str, str]:
        return json.loads(self._fernet.decrypt(token.encode("utf-8")))


def build_master_cipher(master_key: str | None) -> MasterCipher:
    if not master_key:
        raise SaltKeyMissing("SALT_KEY is not configured")
    return MasterCipher(master_key)
