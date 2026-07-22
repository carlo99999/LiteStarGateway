"""version shared routers and bind candidates to model identities

Revision ID: a61d7e3c9b20
Revises: f52a1c9d0b34
Create Date: 2026-07-22 17:45:00
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC
from alembic import op

revision = "a61d7e3c9b20"
down_revision = "f52a1c9d0b34"
branch_labels = None
depends_on = None


def _uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, (bytes, bytearray)):
        return UUID(bytes=bytes(value))
    return UUID(str(value))


def _db_uuid(bind: sa.Connection, value: object) -> UUID | bytes:
    parsed = _uuid(value)
    return parsed.bytes if bind.dialect.name == "sqlite" else parsed


def _lock_sources(bind: sa.Connection) -> None:
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE router, router_grant, model, model_grant, callable_alias IN SHARE MODE"
            )
        )
    elif bind.dialect.name == "sqlite":
        bind.execute(sa.text("UPDATE router SET name = name WHERE 0"))


def _alias_rows(bind: sa.Connection) -> list[sa.RowMapping]:
    return list(
        bind.execute(
            sa.text(
                """
                SELECT ca.id, ca.team_id, ca.alias, ca.unavailable, ca.model_id,
                       ca.model_grant_id, m.team_id AS model_team_id,
                       m.origin_team_id AS model_origin_team_id
                  FROM callable_alias AS ca
                  LEFT JOIN model AS m ON m.id = ca.model_id
                 WHERE ca.router_id IS NULL
                 ORDER BY ca.alias, ca.id
                """
            )
        ).mappings()
    )


def _effective_models(rows: list[sa.RowMapping], team_id: UUID | None) -> dict[str, sa.RowMapping]:
    scoped = [row for row in rows if row["team_id"] in (team_id, None)]
    reserved = {row["alias"] for row in scoped if row["unavailable"]}
    local = [
        row
        for row in scoped
        if team_id is not None
        and row["team_id"] == team_id
        and not row["unavailable"]
        and row["model_id"] is not None
    ]
    globals_ = [
        row
        for row in scoped
        if row["team_id"] is None and not row["unavailable"] and row["model_id"] is not None
    ]
    result = {row["alias"]: row for row in local}
    occupied = set(result) | reserved
    declared_globals = {row["alias"] for row in globals_}
    for row in globals_:
        alias = row["alias"]
        if alias in occupied:
            alias = f"{alias}-global"
            suffix = 2
            while alias in occupied or alias in declared_globals:
                alias = f"{row['alias']}-global-{suffix}"
                suffix += 1
        result[alias] = row
        occupied.add(alias)
    return result


def _bind_strategy(
    strategy: str,
    config: dict[str, Any],
    aliases: dict[str, sa.RowMapping],
    candidate_ids: dict[str, UUID],
    path: str,
    errors: list[str],
) -> dict[str, Any]:
    bound = dict(config)
    if strategy == "judge":
        name = config.get("judge_model")
        row = aliases.get(name) if isinstance(name, str) else None
        if row is None:
            errors.append(f"{path}.judge_model={name!r} is not resolvable")
        else:
            bound["judge_model_id"] = str(row["model_id"])
    elif strategy == "embeddings":
        name = config.get("embedding_model")
        row = aliases.get(name) if isinstance(name, str) else None
        if row is None:
            errors.append(f"{path}.embedding_model={name!r} is not resolvable")
        else:
            bound["embedding_model_id"] = str(row["model_id"])
        routes = []
        for index, route in enumerate(config.get("routes", [])):
            target = route.get("target_model") if isinstance(route, dict) else None
            model_id = candidate_ids.get(target) if isinstance(target, str) else None
            if model_id is None:
                errors.append(f"{path}.routes[{index}].target_model={target!r} is not a candidate")
                routes.append(route)
            else:
                routes.append({**route, "target_model_id": str(model_id)})
        bound["routes"] = routes
    elif strategy == "hybrid":
        escalation = config.get("escalation_strategy")
        nested = config.get("escalation", {})
        if isinstance(escalation, str) and isinstance(nested, dict):
            bound["escalation"] = _bind_strategy(
                escalation, nested, aliases, candidate_ids, f"{path}.escalation", errors
            )
    return bound


def _config_model_ids(value: object) -> set[UUID]:
    found: set[UUID] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_model_id") and item:
                found.add(_uuid(item))
            else:
                found.update(_config_model_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_config_model_ids(item))
    return found


def _preflight(bind: sa.Connection) -> list[dict[str, Any]]:
    aliases = _alias_rows(bind)
    routers = list(
        bind.execute(
            sa.text(
                "SELECT id, team_id, name, candidates, default_model, strategy, "
                "strategy_config, shadow_strategy, enabled FROM router ORDER BY id"
            )
        ).mappings()
    )
    grants = list(
        bind.execute(
            sa.text("SELECT id, router_id, team_id FROM router_grant ORDER BY id")
        ).mappings()
    )
    grants_by_router: dict[UUID, list[sa.RowMapping]] = defaultdict(list)
    for grant in grants:
        grants_by_router[grant["router_id"]].append(grant)

    payload: list[dict[str, Any]] = []
    errors: list[str] = []
    for router in routers:
        router_id = router["id"]
        owner_aliases = _effective_models(aliases, router["team_id"])
        raw_candidates = router["candidates"]
        if isinstance(raw_candidates, str):
            raw_candidates = json.loads(raw_candidates)
        bound_candidates: list[dict[str, Any]] = []
        candidate_ids: dict[str, UUID] = {}
        referenced_ids: set[UUID] = set()
        for index, candidate in enumerate(raw_candidates or []):
            name = candidate.get("model_name") if isinstance(candidate, dict) else None
            row = owner_aliases.get(name) if isinstance(name, str) else None
            if row is None:
                errors.append(
                    f"router={router_id} candidates[{index}] alias={name!r} is not resolvable"
                )
                continue
            if router["team_id"] is None and row["model_team_id"] is not None:
                errors.append(f"global router={router_id} candidate={name!r} is not global")
                continue
            model_id = _uuid(row["model_id"])
            candidate_ids[name] = model_id
            referenced_ids.add(model_id)
            bound_candidates.append(
                {
                    **candidate,
                    "model_id": str(model_id),
                    "model_origin": (
                        "global"
                        if row["model_team_id"] is None
                        else "own"
                        if row["model_team_id"] == router["team_id"]
                        else "extended"
                    ),
                    "source_team_id": str(
                        _uuid(row["model_origin_team_id"] or row["model_team_id"])
                    )
                    if row["model_origin_team_id"] or row["model_team_id"]
                    else None,
                }
            )
        default_id = candidate_ids.get(router["default_model"])
        if default_id is None:
            errors.append(
                f"router={router_id} default_model={router['default_model']!r} is not a candidate"
            )
        config = router["strategy_config"]
        if isinstance(config, str):
            config = json.loads(config)
        bound_config = _bind_strategy(
            router["strategy"], config or {}, owner_aliases, candidate_ids, "active", errors
        )
        if router["shadow_strategy"]:
            shadow = _bind_strategy(
                router["shadow_strategy"],
                (config or {}).get("shadow", {}),
                owner_aliases,
                candidate_ids,
                "shadow",
                errors,
            )
            bound_config = {**bound_config, "shadow": shadow}
        referenced_ids.update(_config_model_ids(bound_config))
        if router["team_id"] is None:
            model_scope = {
                _uuid(row["model_id"]): row["model_team_id"] for row in owner_aliases.values()
            }
            nonglobal = sorted(
                str(model_id)
                for model_id in referenced_ids
                if model_scope.get(model_id) is not None
            )
            if nonglobal:
                errors.append(
                    f"global router={router_id} has non-global dependency model ids {nonglobal}"
                )
        for grant in grants_by_router.get(router_id, []):
            target = _effective_models(aliases, grant["team_id"])
            accessible_ids = {_uuid(row["model_id"]) for row in target.values()}
            missing = sorted(str(model_id) for model_id in referenced_ids - accessible_ids)
            if missing:
                errors.append(
                    f"router_grant={grant['id']} target={grant['team_id']} "
                    f"lacks model ids {missing}"
                )
        payload.append(
            {
                "id": uuid4(),
                "router_id": _uuid(router_id),
                "revision_number": 1,
                "candidates": bound_candidates,
                "default_model_id": default_id,
                "default_model_name": router["default_model"],
                "strategy": router["strategy"],
                "strategy_config": bound_config,
                "shadow_strategy": router["shadow_strategy"],
                "enabled": router["enabled"],
            }
        )
    if errors:
        raise RuntimeError(
            "Router revision migration preflight failed; remediate before retrying:\n"
            + "\n".join(errors[:100])
        )
    return payload


def upgrade() -> None:
    bind = op.get_bind()
    _lock_sources(bind)
    revisions = _preflight(bind)

    op.create_table(
        "router_revision",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("router_id", GUID(length=16), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("default_model_id", GUID(length=16), nullable=False),
        sa.Column("default_model_name", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("strategy_config", sa.JSON(), nullable=False),
        sa.Column("shadow_strategy", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["router.id"],
            name=op.f("fk_router_revision_router_id_router"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_router_revision")),
        sa.UniqueConstraint(
            "router_id", "revision_number", name=op.f("uq_router_revision_router_id")
        ),
    )
    op.create_index(op.f("ix_router_revision_router_id"), "router_revision", ["router_id"])

    with op.batch_alter_table("router", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_revision_id", GUID(length=16), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_router_current_revision_id"), ["current_revision_id"], unique=False
        )
    with op.batch_alter_table("router_grant", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_router_grant_router_id_router"), type_="foreignkey")
        batch_op.create_foreign_key(
            batch_op.f("fk_router_grant_router_id_router"),
            "router",
            ["router_id"],
            ["id"],
        )
        batch_op.add_column(sa.Column("revision_id", GUID(length=16), nullable=True))
        batch_op.add_column(
            sa.Column(
                "ack_active_prompt_egress", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "ack_shadow_prompt_egress", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_router_grant_revision_id_router_revision"),
            "router_revision",
            ["revision_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_router_grant_revision_id"), ["revision_id"], unique=False
        )
    with op.batch_alter_table("routing_decision", schema=None) as batch_op:
        batch_op.add_column(sa.Column("router_revision_id", GUID(length=16), nullable=True))
        batch_op.add_column(sa.Column("chosen_model_id", GUID(length=16), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_routing_decision_router_revision_id"),
            ["router_revision_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_routing_decision_chosen_model_id"),
            ["chosen_model_id"],
            unique=False,
        )

    table = sa.table(
        "router_revision",
        sa.column("id", GUID(length=16)),
        sa.column("router_id", GUID(length=16)),
        sa.column("revision_number", sa.Integer()),
        sa.column("candidates", sa.JSON()),
        sa.column("default_model_id", GUID(length=16)),
        sa.column("default_model_name", sa.String()),
        sa.column("strategy", sa.String()),
        sa.column("strategy_config", sa.JSON()),
        sa.column("shadow_strategy", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("created_at", DateTimeUTC(timezone=True)),
        sa.column("updated_at", DateTimeUTC(timezone=True)),
    )
    now = datetime.now(UTC)
    if revisions:
        op.bulk_insert(table, [{**row, "created_at": now, "updated_at": now} for row in revisions])
    for row in revisions:
        bind.execute(
            sa.text("UPDATE router SET current_revision_id = :revision_id WHERE id = :router_id"),
            {
                "revision_id": _db_uuid(bind, row["id"]),
                "router_id": _db_uuid(bind, row["router_id"]),
            },
        )
        bind.execute(
            sa.text(
                "UPDATE router_grant SET revision_id = :revision_id WHERE router_id = :router_id"
            ),
            {
                "revision_id": _db_uuid(bind, row["id"]),
                "router_id": _db_uuid(bind, row["router_id"]),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("LOCK TABLE router, router_grant, router_revision IN ACCESS EXCLUSIVE MODE")
        )
    elif bind.dialect.name == "sqlite":
        bind.execute(
            sa.text("UPDATE router_revision SET revision_number = revision_number WHERE 0")
        )
    histories = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM (SELECT router_id FROM router_revision "
            "GROUP BY router_id HAVING COUNT(*) > 1) AS histories"
        )
    )
    divergent = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM router_grant AS g JOIN router AS r ON r.id = g.router_id "
            "WHERE g.revision_id IS NOT r.current_revision_id"
        )
    )
    if histories or divergent:
        raise RuntimeError(
            "Router revision downgrade blocked: immutable history or a grant pinned away "
            "from the router head cannot be represented by the legacy schema."
        )
    with op.batch_alter_table("routing_decision", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_routing_decision_chosen_model_id"))
        batch_op.drop_index(batch_op.f("ix_routing_decision_router_revision_id"))
        batch_op.drop_column("chosen_model_id")
        batch_op.drop_column("router_revision_id")
    with op.batch_alter_table("router_grant", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_router_grant_revision_id"))
        batch_op.drop_constraint(
            batch_op.f("fk_router_grant_revision_id_router_revision"), type_="foreignkey"
        )
        batch_op.drop_column("ack_shadow_prompt_egress")
        batch_op.drop_column("ack_active_prompt_egress")
        batch_op.drop_column("revision_id")
        batch_op.drop_constraint(batch_op.f("fk_router_grant_router_id_router"), type_="foreignkey")
        batch_op.create_foreign_key(
            batch_op.f("fk_router_grant_router_id_router"),
            "router",
            ["router_id"],
            ["id"],
            ondelete="CASCADE",
        )
    with op.batch_alter_table("router", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_router_current_revision_id"))
        batch_op.drop_column("current_revision_id")
    op.drop_index(op.f("ix_router_revision_router_id"), table_name="router_revision")
    op.drop_table("router_revision")
