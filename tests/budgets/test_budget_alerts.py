"""Pure logic for Plan 07 Phase 0 — proactive budget alerts.

Covers the threshold-crossing evaluation helper (`crossed_thresholds`) and the
boundary validation for the per-team threshold list (`validate_thresholds`).
Both are pure (no I/O) so they're exercised directly; wiring into
`UsageMeter.settle_ok` and persistence of the dedup ledger is Phase 1+.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from litestar_gateway.domain.budget import crossed_thresholds, validate_thresholds
from litestar_gateway.domain.exceptions import InvalidBudget


class TestCrossedThresholds:
    def test_single_threshold_crossed_and_unfired_fires_once(self) -> None:
        assert crossed_thresholds(
            spend=Decimal("80"), limit_cost=Decimal("100"), thresholds=[50, 80, 100], fired=set()
        ) == [50, 80]

    def test_already_fired_thresholds_are_not_returned_again(self) -> None:
        assert (
            crossed_thresholds(
                spend=Decimal("85"),
                limit_cost=Decimal("100"),
                thresholds=[50, 80, 100],
                fired={50, 80},
            )
            == []
        )

    def test_fresh_empty_fired_set_simulates_period_rollover(self) -> None:
        """A new period_start means a caller-supplied empty fired set — the
        helper must treat that identically to a brand-new team, re-firing
        every currently-crossed threshold."""
        assert crossed_thresholds(
            spend=Decimal("85"), limit_cost=Decimal("100"), thresholds=[50, 80, 100], fired=set()
        ) == [50, 80]

    def test_multiple_thresholds_crossed_in_one_settlement_all_fire(self) -> None:
        """Spend jumps from 40% to 95% in one settlement: 50 and 80 both cross,
        not just the nearest one."""
        assert crossed_thresholds(
            spend=Decimal("95"), limit_cost=Decimal("100"), thresholds=[50, 80, 100], fired=set()
        ) == [50, 80]

    def test_calling_twice_with_same_inputs_is_idempotent(self) -> None:
        """Core correctness property: re-evaluating identical spend/cap/
        thresholds/fired-set returns nothing new the second time, once the
        first call's results are folded into the fired set."""
        first = crossed_thresholds(
            spend=Decimal("80"), limit_cost=Decimal("100"), thresholds=[50, 80, 100], fired=set()
        )
        assert first == [50, 80]
        second = crossed_thresholds(
            spend=Decimal("80"),
            limit_cost=Decimal("100"),
            thresholds=[50, 80, 100],
            fired=set(first),
        )
        assert second == []

    def test_nothing_fires_below_the_lowest_threshold(self) -> None:
        assert (
            crossed_thresholds(
                spend=Decimal("10"),
                limit_cost=Decimal("100"),
                thresholds=[50, 80, 100],
                fired=set(),
            )
            == []
        )

    def test_hitting_cap_fires_the_100_percent_threshold(self) -> None:
        assert crossed_thresholds(
            spend=Decimal("100"), limit_cost=Decimal("100"), thresholds=[50, 80, 100], fired=set()
        ) == [50, 80, 100]

    def test_zero_or_negative_cap_never_fires(self) -> None:
        """A misconfigured (non-positive) cap must not divide-by-zero or
        spuriously fire every threshold."""
        assert (
            crossed_thresholds(
                spend=Decimal("10"), limit_cost=Decimal("0"), thresholds=[50, 80], fired=set()
            )
            == []
        )

    def test_empty_threshold_list_never_fires(self) -> None:
        assert (
            crossed_thresholds(
                spend=Decimal("100"), limit_cost=Decimal("100"), thresholds=[], fired=set()
            )
            == []
        )

    def test_result_preserves_ascending_threshold_order(self) -> None:
        assert crossed_thresholds(
            spend=Decimal("100"), limit_cost=Decimal("100"), thresholds=[100, 50, 80], fired=set()
        ) == [50, 80, 100]


class TestValidateThresholds:
    def test_valid_thresholds_pass_through_sorted_and_deduped(self) -> None:
        assert validate_thresholds([80, 50, 50, 100]) == [50, 80, 100]

    def test_empty_list_is_valid(self) -> None:
        assert validate_thresholds([]) == []

    def test_boundary_values_1_and_100_are_valid(self) -> None:
        assert validate_thresholds([1, 100]) == [1, 100]

    @pytest.mark.parametrize("bad", [0, -1, 101, 1000])
    def test_out_of_range_values_raise(self, bad: int) -> None:
        with pytest.raises(InvalidBudget):
            validate_thresholds([50, bad])

    def test_non_int_values_raise(self) -> None:
        with pytest.raises(InvalidBudget):
            validate_thresholds([50, 80.5])  # type: ignore[list-item]

    def test_booleans_are_rejected_despite_being_ints(self) -> None:
        with pytest.raises(InvalidBudget):
            validate_thresholds([True])  # type: ignore[list-item]
