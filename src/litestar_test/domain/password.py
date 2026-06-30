"""Password hashing (Argon2 via pwdlib).

Unlike API keys (high-entropy random tokens), passwords are low-entropy and must
use a slow, salted KDF. `verify` is constant-time and tolerates rehash needs.
"""

from __future__ import annotations

from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    return _hasher.verify(plaintext, password_hash)
