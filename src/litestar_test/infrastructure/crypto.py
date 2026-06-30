"""Symmetric encryption for credential values, keyed by the salt key.

A Fernet key (AES-128-CBC + HMAC) is derived from the salt key via SHA-256, so
any salt-key string works. Ciphertext is opaque base64 and self-authenticating.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet

from litestar_test.domain.exceptions import SaltKeyMissing


def _derive_fernet_key(salt_key: str) -> bytes:
    digest = hashlib.sha256(salt_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class CredentialCipher:
    def __init__(self, salt_key: str) -> None:
        self._fernet = Fernet(_derive_fernet_key(salt_key))

    def encrypt(self, values: dict[str, str]) -> str:
        return self._fernet.encrypt(json.dumps(values).encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> dict[str, str]:
        return json.loads(self._fernet.decrypt(token.encode("utf-8")))


def build_cipher(salt_key: str | None) -> CredentialCipher:
    if not salt_key:
        raise SaltKeyMissing("SALT_KEY is not configured")
    return CredentialCipher(salt_key)
