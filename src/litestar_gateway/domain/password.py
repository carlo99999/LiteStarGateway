"""Password hashing (Argon2 via pwdlib).

Unlike API keys (high-entropy random tokens), passwords are low-entropy and must
use a slow, salted KDF. `verify` is constant-time and tolerates rehash needs.

Argon2 is CPU-bound (tens to hundreds of ms by design), so async callers must
use the `a*` variants, which offload to a worker thread — calling the sync
functions directly from a coroutine stalls the event loop for every in-flight
request.
"""

from __future__ import annotations

import anyio.to_thread
from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    return _hasher.verify(plaintext, password_hash)


async def ahash_password(plaintext: str) -> str:
    return await anyio.to_thread.run_sync(hash_password, plaintext)


async def averify_password(plaintext: str, password_hash: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, plaintext, password_hash)
