"""Round 10 ISSUE-008: guard against a new user_account FK silently breaking
DELETE /users/{id}.

UserService.delete_user only knows about the foreign keys that exist today
(it blocks on team memberships and created API keys, and clears password
resets). If someone adds a new table with an FK to user_account without
updating that guard, deleting a referenced user would raise an unhandled
IntegrityError (500). This test fails the moment the FK set changes, forcing
the guard to be reconsidered."""

from __future__ import annotations

from advanced_alchemy.extensions.litestar import base

from litestar_gateway.infrastructure.persistence import orm  # noqa: F401 - registers models

# Every FK pointing at user_account today, each handled by delete_user:
#   api_key.created_by      → guard blocks the delete (UserHasReferences)
#   team_membership.user_id → guard blocks the delete (UserHasReferences)
#   password_reset.user_id  → cleared inside the delete
#   mcp_server_proposal.proposed_by → ON DELETE SET NULL, handled by the database
#   mcp_server_proposal.decided_by  → ON DELETE SET NULL, handled by the database
#
# The two MCP columns are the first entries here the *database* clears rather than
# the service. That is deliberate: a proposal is a record of a decision, and a
# decision outliving the account that made it is the audit-shaped answer — blocking
# the delete instead would make a resolved queue a reason a user cannot be removed.
# `test_deleting_a_user_who_filed_a_proposal_keeps_the_proposal` proves the delete
# actually succeeds, because "SET NULL is declared" and "the delete works" are two
# different claims and only the second one matters here.
_KNOWN_USER_ACCOUNT_FKS = {
    "api_key.created_by",
    "mcp_server_proposal.decided_by",
    "mcp_server_proposal.proposed_by",
    "password_reset.user_id",
    "team_membership.user_id",
}

# The FKs above that the database clears on its own, rather than the service. Kept
# separate so a new one cannot be added to the set above without saying which of
# the two mechanisms handles it.
_DATABASE_CLEARED_FKS = {
    "mcp_server_proposal.decided_by",
    "mcp_server_proposal.proposed_by",
}


def test_user_account_foreign_keys_are_all_handled_by_delete_user() -> None:
    metadata = base.UUIDAuditBase.metadata
    actual = {
        f"{table.name}.{fk.parent.name}"
        for table in metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "user_account"
    }
    assert actual == _KNOWN_USER_ACCOUNT_FKS, (
        "A foreign key to user_account changed. Update UserService.delete_user "
        "(and this set) so the new reference is guarded or cleaned, otherwise "
        f"deleting a referenced user raises an unhandled 500. Now: {sorted(actual)}"
    )


def test_every_database_cleared_user_fk_really_declares_on_delete_set_null() -> None:
    """The claim above, checked against the schema rather than trusted.

    A column listed as "the database clears it" but declared without `ondelete`
    would block the delete with an IntegrityError instead — a 500 on an endpoint the
    other half of this module exists to protect.
    """
    metadata = base.UUIDAuditBase.metadata
    declared = {
        f"{table.name}.{fk.parent.name}": fk.ondelete
        for table in metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == "user_account"
    }

    for column in sorted(_DATABASE_CLEARED_FKS):
        assert declared.get(column) == "SET NULL", (
            f"{column} is listed as cleared by the database but declares "
            f"ondelete={declared.get(column)!r}; deleting a referenced user would "
            "fail instead of nulling it."
        )
