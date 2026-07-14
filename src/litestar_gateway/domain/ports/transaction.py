"""Port — unit-of-work transaction boundary."""

from __future__ import annotations

from typing import Protocol


class Transaction(Protocol):
    """Unit-of-work boundary for a single use-case.

    Repositories participating in a transactional flow only *stage* writes
    (flush, no commit); the service commits once via this port, so a multi-step
    operation either persists fully or not at all. `AsyncSession` satisfies it.

    Project convention: a repository method that is the *only* write of its
    use case may self-commit (most single-write CRUD does); any use case with
    two or more writes MUST stage them and commit once through this port
    (see `TeamService` and `UserService.register`). Deliberate exceptions are
    documented at the call site (e.g. the usage ledger writes autonomously so
    billing is fail-safe).
    """

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
