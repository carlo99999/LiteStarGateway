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
_KNOWN_USER_ACCOUNT_FKS = {
    "api_key.created_by",
    "password_reset.user_id",
    "team_membership.user_id",
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
