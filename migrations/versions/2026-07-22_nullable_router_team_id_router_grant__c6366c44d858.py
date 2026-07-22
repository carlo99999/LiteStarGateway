"""nullable router.team_id + router_grant + origin

Revision ID: c6366c44d858
Revises: b213468f39d2
Create Date: 2026-07-22 11:45:56.056968

"""

import warnings
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import context, op
from advanced_alchemy.types import (
    Bool,
    EncryptedString,
    EncryptedText,
    GUID,
    JsonB,
    ORA_JSONB,
    DateTimeUTC,
    StoredObject,
    PasswordHash,
    FernetBackend,
    TOTPSecret,
    OneTimeCode,
)
from advanced_alchemy.types.encrypted_string import PGCryptoBackend
from advanced_alchemy.types.password_hash.argon2 import Argon2Hasher
from advanced_alchemy.types.password_hash.passlib import PasslibHasher
from advanced_alchemy.types.password_hash.pwdlib import PwdlibHasher
from sqlalchemy import Text  # noqa: F401


if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = (
    "downgrade",
    "upgrade",
    "schema_upgrades",
    "schema_downgrades",
    "data_upgrades",
    "data_downgrades",
)

sa.GUID = GUID
sa.Bool = Bool
sa.DateTimeUTC = DateTimeUTC
sa.JsonB = JsonB
sa.ORA_JSONB = ORA_JSONB
sa.EncryptedString = EncryptedString
sa.EncryptedText = EncryptedText
sa.StoredObject = StoredObject
sa.PasswordHash = PasswordHash
sa.Argon2Hasher = Argon2Hasher
sa.PasslibHasher = PasslibHasher
sa.PwdlibHasher = PwdlibHasher
sa.FernetBackend = FernetBackend
sa.PGCryptoBackend = PGCryptoBackend
sa.TOTPSecret = TOTPSecret
sa.OneTimeCode = OneTimeCode

# revision identifiers, used by Alembic.
revision = "c6366c44d858"
down_revision = "b213468f39d2"
branch_labels = None
depends_on = None

_MODEL_ORIGIN_REVISION = "b213468f39d2"


def _downgrade_includes_model_provenance() -> bool:
    """Whether this Alembic invocation will also downgrade b213468f39d2.

    ``get_revision_argument`` is Alembic's public destination API.  Traversing
    from this revision to that destination also resolves the common relative
    ``-1`` form.  Any missing/ambiguous context fails closed: an unnecessary
    preflight is safer than applying router DDL before discovering unsafe model
    data in the following revision.
    """
    migration_context = context.get_context()
    script = migration_context.script
    if script is None:
        return True
    destination = context.get_revision_argument()
    # If this migration is running for a one-step relative downgrade, the
    # destination is its direct parent: model provenance remains intact.
    if destination == "-1":
        return False
    try:
        return any(
            item.revision == _MODEL_ORIGIN_REVISION
            for item in script.iterate_revisions(revision, destination)
        )
    except Exception:
        return True


def upgrade() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        with op.get_context().autocommit_block():
            schema_upgrades()
            data_upgrades()


def downgrade() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        with op.get_context().autocommit_block():
            data_downgrades()
            schema_downgrades()


def schema_upgrades() -> None:
    """schema upgrade migrations go here."""
    # Self-healing (see the model_grant migration): tolerate a router_grant /
    # index already built by a prior create_all so `database upgrade` succeeds.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "router_grant" not in tables:
        op.create_table(
            "router_grant",
            sa.Column("id", sa.GUID(length=16), nullable=False),
            sa.Column("router_id", sa.GUID(length=16), nullable=False),
            sa.Column("team_id", sa.GUID(length=16), nullable=False),
            sa.Column("alias", sa.String(), nullable=False),
            sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTimeUTC(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTimeUTC(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["router_id"],
                ["router.id"],
                name=op.f("fk_router_grant_router_id_router"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["team_id"], ["team.id"], name=op.f("fk_router_grant_team_id_team")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_router_grant")),
            sa.UniqueConstraint("router_id", "team_id", name=op.f("uq_router_grant_router_id")),
            sa.UniqueConstraint("team_id", "alias", name=op.f("uq_router_grant_team_id")),
        )
        with op.batch_alter_table("router_grant", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_router_grant_router_id"), ["router_id"], unique=False
            )
            batch_op.create_index(batch_op.f("ix_router_grant_team_id"), ["team_id"], unique=False)

    router_columns = {c["name"] for c in inspector.get_columns("router")}
    router_indexes = {ix["name"] for ix in inspector.get_indexes("router")}
    with op.batch_alter_table("router", schema=None) as batch_op:
        if "origin_team_id" not in router_columns:
            batch_op.add_column(sa.Column("origin_team_id", sa.GUID(length=16), nullable=True))
        batch_op.alter_column("team_id", nullable=True)
        if "uq_global_router_name" not in router_indexes:
            batch_op.create_index(
                "uq_global_router_name",
                ["name"],
                unique=True,
                sqlite_where=sa.text("team_id IS NULL"),
                postgresql_where=sa.text("team_id IS NULL"),
            )


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("router", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_global_router_name",
            sqlite_where=sa.text("team_id IS NULL"),
            postgresql_where=sa.text("team_id IS NULL"),
        )
        batch_op.alter_column("team_id", nullable=False)
        batch_op.drop_column("origin_team_id")

    with op.batch_alter_table("router_grant", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_router_grant_team_id"))
        batch_op.drop_index(batch_op.f("ix_router_grant_router_id"))

    op.drop_table("router_grant")
    # ### end Alembic commands ###


def data_upgrades() -> None:
    """Seed provenance: a team-owned router's origin is its own team."""
    op.execute(
        "UPDATE router SET origin_team_id = team_id "
        "WHERE origin_team_id IS NULL AND team_id IS NOT NULL"
    )


def data_downgrades() -> None:
    """Preflight the whole global-resource rollback, then restore ownership.

    Model checks belong here as well as in the earlier model revisions.  Without
    them a downgrade from head could apply the router DDL first and only then
    discover an unsafe native global model while traversing b213468f39d2.
    """
    bind = op.get_bind()
    blockers: list[str] = []

    resources = [("router", "router")]
    if _downgrade_includes_model_provenance():
        resources.insert(0, ("model", "model"))

    for table, singular in resources:
        native = list(
            bind.execute(
                sa.text(
                    f"SELECT name FROM {table} "
                    "WHERE team_id IS NULL AND origin_team_id IS NULL "
                    "ORDER BY name LIMIT 5"
                )
            ).scalars()
        )
        missing_origins = list(
            bind.execute(
                sa.text(
                    f"SELECT resource.name FROM {table} AS resource "
                    "LEFT JOIN team ON team.id = resource.origin_team_id "
                    "WHERE resource.team_id IS NULL AND resource.origin_team_id IS NOT NULL "
                    "AND team.id IS NULL ORDER BY resource.name LIMIT 5"
                )
            ).scalars()
        )
        collisions = list(
            bind.execute(
                sa.text(
                    f"SELECT global_resource.name FROM {table} AS global_resource "
                    f"JOIN {table} AS local_resource "
                    "ON local_resource.team_id = global_resource.origin_team_id "
                    "AND local_resource.name = global_resource.name "
                    "AND local_resource.id <> global_resource.id "
                    "WHERE global_resource.team_id IS NULL "
                    "ORDER BY global_resource.name LIMIT 5"
                )
            ).scalars()
        )
        if native:
            blockers.append(f"native global {singular} without origin_team_id: {native!r}")
        if missing_origins:
            blockers.append(
                f"{singular} origin_team_id does not reference an existing team: "
                f"{missing_origins!r}"
            )
        if collisions:
            blockers.append(
                f"{singular} name already exists in its origin team: {collisions!r}"
            )

    if blockers:
        raise RuntimeError(
            "Cannot downgrade global resources safely: "
            + "; ".join(blockers)
            + ". Reassign or intentionally delete each blocked resource, then retry. "
            "See docs/db-migrations.md#downgrading-global-resources. "
            "No schema DDL from revision c6366c44d858 was applied."
        )

    bind.execute(
        sa.text(
            "UPDATE router SET team_id = origin_team_id "
            "WHERE team_id IS NULL AND origin_team_id IS NOT NULL"
        )
    )
