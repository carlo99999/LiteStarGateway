"""Pure domain logic for generating and hashing keys.

Keys are high-entropy random tokens, so a fast SHA-256 digest is sufficient for
storage (no salting/bcrypt needed). Verification looks up the hash by equality
in the database rather than comparing plaintext, so no constant-time compare
is needed here.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

KEY_PREFIX = "lsk_"  # identifies the token format ("litestar key")
_TOKEN_BYTES = 32
_DISPLAY_PREFIX_LEN = len(KEY_PREFIX) + 8


@dataclass(frozen=True)
class NewKeyMaterial:
    plaintext: str
    prefix: str
    key_hash: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key() -> NewKeyMaterial:
    plaintext = KEY_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    return NewKeyMaterial(
        plaintext=plaintext,
        prefix=plaintext[:_DISPLAY_PREFIX_LEN],
        key_hash=hash_key(plaintext),
    )
