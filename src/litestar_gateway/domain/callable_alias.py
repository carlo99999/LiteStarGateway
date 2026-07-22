"""Stable identities in the shared model/router callable namespace."""

from __future__ import annotations

import dataclasses
from enum import StrEnum
from uuid import UUID


class CallableKind(StrEnum):
    MODEL = "model"
    ROUTER = "router"


class CallableOrigin(StrEnum):
    OWN = "own"
    EXTENDED = "extended"
    GLOBAL = "global"


@dataclasses.dataclass(frozen=True)
class CallableAliasBinding:
    id: UUID
    team_id: UUID | None
    alias: str
    kind: CallableKind
    resource_id: UUID
    origin: CallableOrigin
    source_team_id: UUID | None
    router_grant_id: UUID | None = None
    router_revision_id: UUID | None = None
