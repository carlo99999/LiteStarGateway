"""Access to a real Redis, when one is available.

The strict `FakeRedis` in `support.doubles` is enough for logic that merely
*uses* Redis. It is not enough for logic whose correctness IS Redis semantics —
TTL expiry, `SET NX`, script atomicity. ISSUE-029 was exactly that: a TTL the
fake recorded and never applied, keeping a broken adapter green. Those tests
belong here and skip when no server is configured, so a local run without
Docker stays possible; CI's `checks` job always sets `REDIS_TEST_URL`.
"""

from __future__ import annotations

import os

import pytest

REDIS_TEST_URL = os.environ.get("REDIS_TEST_URL")

requires_redis = pytest.mark.skipif(
    not REDIS_TEST_URL,
    reason="no REDIS_TEST_URL: run `just test-redis`, or let CI's `checks` job cover it",
)
