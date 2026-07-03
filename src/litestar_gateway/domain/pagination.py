"""Bounded pagination for list operations.

Keeps collection reads from loading an unbounded number of rows into memory: list
queries always run with a limit, and clients page through with `limit`/`offset`.
"""

from __future__ import annotations

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def resolve_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    """Clamp client-supplied paging to a sane, bounded window (limit in
    [1, MAX_PAGE_SIZE], offset >= 0), applying the default when unset."""
    resolved_limit = DEFAULT_PAGE_SIZE if limit is None else max(1, min(limit, MAX_PAGE_SIZE))
    resolved_offset = 0 if offset is None else max(0, offset)
    return resolved_limit, resolved_offset
