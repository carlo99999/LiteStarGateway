"""Shared test doubles.

Rounds 13 and 14 both produced defects the suite could not have caught, from one
cause: a double more forgiving than the thing it stands for. `FakeRedis`
recorded TTLs and never applied them, keeping a broken circuit breaker green
(ISSUE-029), and a multi-replica cache test gave each "replica" its own
freshly-minted `Model`, which production never does. Keeping the strict versions
here — rather than hand-rolling one per test module — is what makes strict the
default for the next component that needs them.

Imported as `from support.doubles import ...`: pytest puts `tests/` on
`sys.path`, the same mechanism `_invite_helpers` relies on.
"""

from support.doubles import FakeRedis, MutableClock
from support.sessions import two_sessions_over_one_database

__all__ = ["FakeRedis", "MutableClock", "two_sessions_over_one_database"]
