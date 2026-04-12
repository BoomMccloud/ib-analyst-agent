"""
Unit Tests: pymodel.py internals
================================
Fine-grained tests for fv(), role-tag lookups, and cash begin edge cases.
Complements the integration tests in test_merge_pipeline.py.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, find_node_by_role
from pymodel import verify_model, _verify_segment_sums
from merge_trees import _recompute_residuals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_leaf(concept, weight=1.0, values=None, role=None):
    node = TreeNode(concept, weight)
    node.values = values or {}
    if role:
        node.role = role
    return node


def _make_parent(concept, children, weight=1.0, values=None, role=None):
    node = TreeNode(concept, weight)
    node.values = values or {}
    if role:
        node.role = role
    for c in children:
        node.add_child(c)
    return node


# ===========================================================================
# Unit Test 1: fv() returns leaf value
# ===========================================================================

class TestFvReturnsLeafValue:
    def test_fv_returns_leaf_value(self):
        """fv(leaf, period) should return the node's declared value."""
        leaf = _make_leaf("Revenue", values={"2024": 1000})

        # Build minimal trees to call verify_model and inspect fv behavior
        # We test fv indirectly: a leaf-only IS with role INC_NET and a leaf-only
        # CF with INC_NET_CF. If fv works on leaves, NI Link passes when values match.
        is_tree = _make_parent("IS", [
            _make_leaf("NetIncome", values={"2024": 500}, role="INC_NET"),
        ], role="IS_ROOT")
        cf_tree = _make_parent("CF", [
            _make_leaf("NetIncomeCF", values={"2024": 500}, role="INC_NET_CF"),
        ], role="CF_ROOT")

        trees = {
            "IS": is_tree,
            "BS": _make_parent("BS", [_make_leaf("Cash", values={"2024": 100}, role="BS_TA")], role="BS_ROOT"),
            "BS_LE": _make_parent("BSLE", [_make_leaf("TL", values={"2024": 50}, role="BS_TL"),
                                             _make_leaf("TE", values={"2024": 50}, role="BS_TE")]),
            "CF": cf_tree,
            "complete_periods": ["2024"],
            "cf_endc_values": {},
        }
        errors = verify_model(trees)
        # NI Link should pass (both leaves = 500), proving fv returns leaf value
        ni_errors = [e for e in errors if "NI Link" in e[0]]
        assert ni_errors == [], f"NI Link should pass for matching leaves: {ni_errors}"


# ===========================================================================
# Unit Test 2: fv() returns sum of children, not declared value
# ===========================================================================

class TestFvReturnsSumOfChildren:
    def test_fv_returns_sum_of_children(self):
        """fv(parent) should return SUM(fv(children)*weight), not parent.values."""
        # Parent declares 999 but children sum to 500+300=800
        child_a = _make_leaf("ChildA", values={"2024": 500})
        child_b = _make_leaf("ChildB", values={"2024": 300})
        parent = _make_parent("Parent", [child_a, child_b], values={"2024": 999})

        # Use _verify_segment_sums to test fv behavior on parent with children
        errors = []
        _verify_segment_sums(parent, ["2024"], errors)
        # fv(parent) should be 800 (from children), declared is 999, delta = 999 - 800 = 199
        assert len(errors) == 1
        assert abs(errors[0][2] - 199) < 0.01


# ===========================================================================
# Unit Test 3: fv() respects negative weights
# ===========================================================================

class TestFvRespectsNegativeWeights:
    def test_fv_respects_negative_weights(self):
        """A child with weight=-1 should be subtracted by fv()."""
        pos_child = _make_leaf("Revenue", values={"2024": 1000})
        neg_child = _make_leaf("Expenses", weight=-1.0, values={"2024": 400})
        parent = _make_parent("GrossProfit", [pos_child, neg_child],
                              values={"2024": 600})

        # fv(parent) = 1000*1 + 400*(-1) = 600. Declared = 600. Delta = 0.
        errors = []
        _verify_segment_sums(parent, ["2024"], errors)
        assert errors == [], f"Should pass when declared matches formula with neg weight: {errors}"

    def test_fv_negative_weight_mismatch(self):
        """Verify that a mismatch with negative weight is caught."""
        pos_child = _make_leaf("Revenue", values={"2024": 1000})
        neg_child = _make_leaf("Expenses", weight=-1.0, values={"2024": 400})
        parent = _make_parent("GrossProfit", [pos_child, neg_child],
                              values={"2024": 500})  # declared 500, formula=600

        errors = []
        _verify_segment_sums(parent, ["2024"], errors)
        assert len(errors) == 1
        assert abs(errors[0][2] - (-100)) < 0.01  # 500 - 600 = -100


# ===========================================================================
# Unit Test 4: find_node_by_role returns None when missing
# ===========================================================================

class TestDaRoleTagLookupReturnsNoneWhenMissing:
    def test_da_role_tag_lookup_returns_none_when_missing(self):
        """When no node has role IS_DA, find_node_by_role returns None
        and verify_model silently skips the D&A check (no error, no crash)."""
        # Build trees with NO D&A role tags
        is_tree = _make_parent("IS", [
            _make_leaf("Revenue", values={"2024": 1000}, role="IS_REVENUE"),
            _make_leaf("NetIncome", values={"2024": 200}, role="INC_NET"),
        ])
        cf_tree = _make_parent("CF", [
            _make_leaf("NetIncomeCF", values={"2024": 200}, role="INC_NET_CF"),
        ])

        assert find_node_by_role(is_tree, "IS_DA") is None
        assert find_node_by_role(cf_tree, "CF_DA") is None

        trees = {
            "IS": is_tree,
            "BS": _make_parent("BS", [_make_leaf("TA", values={"2024": 100}, role="BS_TA")]),
            "BS_LE": _make_parent("BSLE", [_make_leaf("TL", values={"2024": 50}, role="BS_TL"),
                                             _make_leaf("TE", values={"2024": 50}, role="BS_TE")]),
            "CF": cf_tree,
            "complete_periods": ["2024"],
            "cf_endc_values": {},
        }
        errors = verify_model(trees)
        da_errors = [e for e in errors if "D&A" in e[0]]
        assert da_errors == [], f"D&A check should be silently skipped: {da_errors}"


# ===========================================================================
# Unit Test 5: Cash Begin skipped for single period
# ===========================================================================

class TestCashBeginSkippedForSinglePeriod:
    def test_cash_begin_skipped_for_single_period(self):
        """With only 1 period, Cash Begin check has no prior period — should produce no errors."""
        bs_tree = _make_parent("BS", [
            _make_leaf("Cash", values={"2024": 500}, role="BS_CASH"),
            _make_leaf("OtherAssets", values={"2024": 500}),
        ], role="BS_TA", values={"2024": 1000})
        cf_tree = _make_parent("CF", [
            _make_leaf("BegCash", values={"2024": 999}, role="CF_BEGC"),  # intentional mismatch
        ])

        trees = {
            "IS": _make_parent("IS", [_make_leaf("NI", values={"2024": 100}, role="INC_NET")]),
            "BS": bs_tree,
            "BS_LE": _make_parent("BSLE", [_make_leaf("TL", values={"2024": 500}, role="BS_TL"),
                                             _make_leaf("TE", values={"2024": 500}, role="BS_TE")]),
            "CF": cf_tree,
            "complete_periods": ["2024"],
            "cf_endc_values": {},
        }
        errors = verify_model(trees)
        cash_begin_errors = [e for e in errors if "Cash Begin" in e[0]]
        assert cash_begin_errors == [], f"Cash Begin should be skipped for single period: {cash_begin_errors}"


# ===========================================================================
# Unit Test 6: Residual warning fires for one period only
# ===========================================================================

class TestResidualWarningMultiplePeriods:
    def test_residual_warning_multiple_periods(self):
        """With 2 periods, warning should fire only for the period with a large residual."""
        child = _make_leaf("ChildA", values={"2023": 100, "2024": 100})
        parent = _make_parent("Parent", [child],
                              values={"2023": 110, "2024": 500})
        # 2023: residual=10, sibling_avg=100 → 10 < 100 → no warning
        # 2024: residual=400, sibling_avg=100 → 400 > 100 → warning

        logger = logging.getLogger("merge_trees")

        with _capture_logs(logger) as records:
            _recompute_residuals(parent, ["2023", "2024"])

        warning_records = [r for r in records if r.levelno >= logging.WARNING]
        # Should have exactly 1 warning (for 2024 only)
        assert len(warning_records) == 1, f"Expected 1 warning, got {len(warning_records)}: {warning_records}"
        assert "2024" in warning_records[0].message
        assert "2023" not in warning_records[0].message


class _capture_logs:
    """Minimal log capture context manager for a specific logger."""
    def __init__(self, logger):
        self.logger = logger
        self.records = []
        self.handler = None

    def __enter__(self):
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.records.append(record)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        return self.records

    def __exit__(self, *args):
        self.logger.removeHandler(self.handler)
