"""Plan 20 S0 — the domain rules that decide whether a tool may be invoked.

These are pure: the interesting behaviour is the default polarity (permissive for
reads, explicit for destructive) and the fact that an unclassified tool is treated
as the most dangerous class rather than the safest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from litestar_gateway.domain.mcp import ApiKeyToolPolicy, McpServer, McpTool, ToolEffect


def _server(**overrides) -> McpServer:
    base: dict = {
        "id": uuid4(),
        "team_id": uuid4(),
        "name": "github",
        "url": "https://tools.internal:8443/mcp",
        "enabled": True,
        "created_at": datetime.now(UTC),
    }
    return McpServer(**(base | overrides))


def _policy(**overrides) -> ApiKeyToolPolicy:
    base: dict = {"id": uuid4(), "api_key_id": uuid4(), "team_id": uuid4()}
    return ApiKeyToolPolicy(**(base | overrides))


def test_an_unclassified_tool_counts_as_destructive() -> None:
    # Declared, never detected: a default of `read` would make an operator who
    # forgot to classify a tool strictly worse off than one who never registered
    # the server at all.
    tool = McpTool(id=uuid4(), server_id=uuid4(), name="delete_repo", description="", schema={})

    assert tool.effect is ToolEffect.DESTRUCTIVE


def test_only_destructive_needs_an_explicit_key_grant() -> None:
    assert ToolEffect.DESTRUCTIVE.needs_explicit_key_grant
    assert not ToolEffect.READ.needs_explicit_key_grant
    assert not ToolEffect.WRITE.needs_explicit_key_grant


def test_a_policy_without_a_tool_list_is_unrestricted_for_reads_and_writes() -> None:
    # The permissive default: the feature works the moment a server is
    # registered, the same polarity a missing spend cap already has.
    policy = _policy()

    assert policy.permits("list_issues", ToolEffect.READ)
    assert policy.permits("create_issue", ToolEffect.WRITE)


def test_the_same_policy_still_refuses_a_destructive_tool() -> None:
    # The one carve-out from the permissive default.
    assert not _policy().permits("delete_repo", ToolEffect.DESTRUCTIVE)


def test_destructive_needs_both_the_flag_and_the_list_when_a_list_exists() -> None:
    enabled = _policy(destructive_enabled=True, allowed_tools=("delete_repo",))

    assert enabled.permits("delete_repo", ToolEffect.DESTRUCTIVE)
    # Enabling destructive does not widen the tool list.
    assert not enabled.permits("drop_database", ToolEffect.DESTRUCTIVE)
    # ...and the list still bounds the harmless classes too.
    assert not enabled.permits("list_issues", ToolEffect.READ)


def test_a_server_allowlist_is_exhaustive_when_present() -> None:
    assert _server().exposes("anything")  # empty ⇒ everything advertised
    narrowed = _server(tool_allowlist=("list_issues",))
    assert narrowed.exposes("list_issues")
    assert not narrowed.exposes("delete_repo")


def test_a_global_server_is_spelled_with_no_team() -> None:
    # Same spelling as a global `Model`, which is what lets the visibility union
    # be written once instead of per resource.
    assert _server(team_id=None).is_global
    assert not _server().is_global
