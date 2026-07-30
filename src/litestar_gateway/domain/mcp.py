"""MCP tool servers: the domain types (Plan 20 S0).

An MCP server is a **team** resource — its admins create and remove it — while
the platform keeps one veto: `MCP_ALLOWED_HOSTS` bounds where any of them may
point. That is the opposite of a `Credential`, deliberately: a credential is
platform-owned because it carries *the platform's* secret, and an MCP server
carries the team's own.

Visibility reuses `CallableOrigin` rather than inventing an enum, so a server can
be the team's own, extended to chosen teams, or global — exactly the three
`Model` already has. The resolution of "which servers can this team see" is
deliberately **not** here: it belongs to one function in the application layer,
because a check spelled `server.team_id == team_id` silently excludes globals and
extended servers, and that spelling has already produced two findings in this
codebase (guardrail scope on global models, and Round 12's ISSUE-020).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from litestar_gateway.domain.callable_alias import CallableOrigin


class ToolEffect(StrEnum):
    """What invoking a tool does, as declared by an operator.

    Declared, never detected — the rule Plan 18 established for model
    capabilities, for the same reason: a value inferred from what a server says
    about itself is a value the server controls. An MCP `annotations` block may
    seed this, but it never decides it, and a tool nobody classified counts as
    `DESTRUCTIVE` until someone says otherwise.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def needs_explicit_key_grant(self) -> bool:
        """`destructive` is the one class a permissive default does not cover.

        Absent per-key policy means unrestricted, the polarity a missing spend
        cap already has — so the feature works the moment a server is
        registered. Destructive tools are carved out of that default: a key
        issued for a low-trust application must not be able to delete something
        because a prompt asked it to.
        """
        return self is ToolEffect.DESTRUCTIVE


@dataclasses.dataclass(frozen=True)
class McpServer:
    """A registered remote MCP server.

    `team_id` is `None` for a global server, matching how a global `Model` is
    spelled. `auth` never appears here: the bearer token lives encrypted in the
    persistence layer and is decrypted only on the call path, so no entity a
    response is built from can carry it — the same shape as a guardrail rule's
    signing secret.
    """

    id: UUID
    team_id: UUID | None
    name: str
    url: str
    enabled: bool
    created_at: datetime
    has_auth: bool = False
    tool_allowlist: tuple[str, ...] = ()
    origin: CallableOrigin = CallableOrigin.OWN
    # When `tools/list` last succeeded. `None` means it never ran — a different
    # state from "it ran and the server offers nothing", which is otherwise the
    # same empty inventory.
    last_discovered_at: datetime | None = None

    @property
    def is_global(self) -> bool:
        return self.team_id is None

    def exposes(self, tool_name: str) -> bool:
        """An empty allowlist exposes everything the server advertises; a
        non-empty one is exhaustive."""
        return not self.tool_allowlist or tool_name in self.tool_allowlist


@dataclasses.dataclass(frozen=True)
class McpServerGrant:
    """A team-owned server extended to another team, mirroring `ModelGrant`.

    Unlike a model grant there is no `alias`: a server is referenced by its own
    name, and the extension is refused outright when the target team already sees
    that name (application layer). Renaming-on-extend is a mechanism this feature
    does not need yet, and inventing one here would put two spellings of the same
    server in front of one team.
    """

    id: UUID
    server_id: UUID
    team_id: UUID  # the team the server is extended to
    created_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class McpTool:
    """One tool discovered from a server, with the effect an operator declared.

    `schema` is the JSON Schema the server advertised for its arguments. It is
    stored as discovered and validated by `domain.chat_tool_policy` at the point
    it becomes a declaration in a model request — the existing limits (count,
    schema bytes, depth, per-provider name rules) apply unchanged rather than
    being re-implemented here.
    """

    id: UUID
    server_id: UUID
    name: str
    description: str
    schema: dict
    effect: ToolEffect = ToolEffect.DESTRUCTIVE
    discovered_at: datetime | None = None


class ProposalStatus(StrEnum):
    """`pending → approved | rejected`, and nothing else.

    A two-state decision rather than a negotiation: an approver takes the
    proposal as filed or rejects it with a reason. Editing it would make this a
    three-party workflow whose intermediate states nobody has to model, and an
    admin who wants different settings can register the server directly.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclasses.dataclass(frozen=True)
class McpServerProposal:
    """A team member's request to register a server (design §2.4).

    Two properties are the whole point of the type, and both are about what a
    proposal is *not*:

    **It is not a server.** Nothing reads it on the call path, so filing one
    changes no policy — which is what makes `tools:propose` safe to hold in every
    role, including `member`, the role that otherwise holds nothing.

    **It is not a promise.** The allowlist membership recorded when it was filed
    is not the allowlist that decides: approval re-checks. A host allowlisted on
    Monday and removed on Tuesday must not become a live server on Wednesday
    because a pending row still remembers it — ISSUE-034's lesson applied to a
    time gap rather than to DNS.

    `auth` is absent here for the same reason it is absent from `McpServer`: the
    token lives encrypted in the persistence layer, so the approver sees the name,
    the url and the requested tools, never the secret.
    """

    id: UUID
    team_id: UUID
    proposed_by: UUID | None
    name: str
    url: str
    status: ProposalStatus = ProposalStatus.PENDING
    tool_allowlist: tuple[str, ...] = ()
    has_auth: bool = False
    # Set on rejection only. "It disappeared" is not an answer for the member who
    # filed it, so a refusal carries why.
    reason: str | None = None
    # The server approval created, when it did. `None` on a pending or rejected
    # proposal — and also on an approved one whose server was later deleted,
    # which is a fact about the server rather than about the decision.
    server_id: UUID | None = None
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.status is ProposalStatus.PENDING


@dataclasses.dataclass(frozen=True)
class ApiKeyToolPolicy:
    """Per-key restriction, on the `api_key_budget` precedent: a policy row read
    on the call path.

    Absent means unrestricted. Present with an empty `allowed_tools` also means
    unrestricted — the row exists to carry `destructive_enabled`, and an operator
    who only wants to enable destructive tools should not have to enumerate every
    read tool to keep them working.
    """

    id: UUID
    api_key_id: UUID
    team_id: UUID
    allowed_tools: tuple[str, ...] = ()
    destructive_enabled: bool = False
    created_at: datetime | None = None

    def permits(self, tool_name: str, effect: ToolEffect) -> bool:
        if effect.needs_explicit_key_grant and not self.destructive_enabled:
            return False
        return not self.allowed_tools or tool_name in self.allowed_tools
