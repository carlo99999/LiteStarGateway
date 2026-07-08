"""R7-M54/M55: index coverage for hot filter columns on api_key and routing_decision.

`revoke_personal_keys_for_user` filters `api_key` by `created_by`, and the
routing_decision repository (`list_decisions`, `distribution`, `savings`) always
filters by `team_id AND router_name`, with `list_decisions` also ordering by
`created_at DESC`. These indexes keep those queries off a full table scan.
"""

from __future__ import annotations

from litestar_gateway.infrastructure.persistence.orm import APIKeyModel, RoutingDecisionModel


def _index_column_names(index: object) -> tuple[str, ...]:
    return tuple(column.name for column in index.columns)  # type: ignore[attr-defined]


def test_api_key_created_by_is_indexed() -> None:
    indexed_columns = {
        column_name
        for index in APIKeyModel.__table__.indexes  # type: ignore[missing-attribute]
        for column_name in _index_column_names(index)
    }

    assert "created_by" in indexed_columns


def test_routing_decision_has_composite_team_router_created_at_index() -> None:
    indexes = RoutingDecisionModel.__table__.indexes  # type: ignore[missing-attribute]

    composite = [
        index
        for index in indexes
        if _index_column_names(index) == ("team_id", "router_name", "created_at")
    ]
    assert len(composite) == 1, (
        f"expected exactly one composite (team_id, router_name, created_at) index, found: "
        f"{[_index_column_names(i) for i in indexes]}"
    )


def test_routing_decision_has_no_independent_single_column_indexes() -> None:
    """The composite index replaces the two single-column indexes (R7-M55)."""
    single_column_index_columns = {
        _index_column_names(index)[0]
        for index in RoutingDecisionModel.__table__.indexes  # type: ignore[missing-attribute]
        if len(_index_column_names(index)) == 1
    }

    assert "team_id" not in single_column_index_columns
    assert "router_name" not in single_column_index_columns
