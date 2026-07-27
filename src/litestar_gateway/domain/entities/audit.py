"""Audit log entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class AuditEvent:
    """An append-only record of a privileged action: who did what, to what, from
    where, and when. `actor_email` is denormalized so the log stays readable even
    if the user is later removed. Never store secrets in `detail`."""

    id: UUID
    action: str  # "<resource>.<verb>", e.g. "credential.create", "api_key.revoke"
    actor_id: UUID | None
    # Disambiguates actor_id ("user" | "api_key"): user ids and key ids share
    # the column, and a reader must never join a key id against users.
    actor_type: str | None
    actor_email: str | None
    target_type: str | None
    target_id: str | None
    ip: str | None
    detail: str | None
    created_at: datetime
    # Request correlation id (Plan 11 Slice A, docs/logging.md §2): the same
    # opaque id carried in the response header and log lines for the request
    # that triggered this action, so it can be followed end-to-end.
    request_id: str | None = None
