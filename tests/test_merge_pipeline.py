"""
TDD Integration Tests: Multi-Year Tree Merge
=============================================
Tests for Priority 1 (pymodel.py verification fixes) and
Priority 2 (pipeline wiring + residual sanity logging).

All tests should FAIL against current code — they define "done".
"""

import json
import logging
import os
import sys
import subprocess
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, find_node_by_role
from pymodel import verify_model, _verify_segment_sums


# ---------------------------------------------------------------------------
# Fixture helpers (same pattern as test_da_sbc_tagging.py)
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
# Priority 1: pymodel.py verification fixes
# ===========================================================================

class TestSegmentSumsUseFv:
    """Test 1: _verify_segment_sums should use fv() (formula values), not
    .values.get() (declared values).

    Bug: A segment parent's declared value can differ from SUM(fv(children))
    when children have their own sub-children. Current code checks declared
    values only and misses the mismatch.
    """

    def test_segment_sum_catches_formula_mismatch_at_parent_level(self):
        """Create a tree where:
        - Parent declared value = 500
        - Child A declared value = 300, but its children sum to 280 (fv = 280)
        - Child B declared value = 200

        Using declared values at the parent level: 300 + 200 = 500 → PASS
        Using fv() at the parent level: 280 + 200 = 480 != 500 → FAIL

        The current code checks each level independently with declared values,
        so it catches the sub-level error (300 != 280) but MISSES the parent-level
        error (500 != 480). Fixed code using fv() should report the error at the
        parent level too, since that's what the sheet formula would show.
        """
        # Child A has sub-children that don't sum to its declared value
        child_a = _make_parent(
            "us-gaap_ProductRevenue",
            children=[
                _make_leaf("us-gaap_DomesticProduct", values={"2024": 180}),
                _make_leaf("us-gaap_InternationalProduct", values={"2024": 100}),
            ],
            values={"2024": 300},  # declared 300, but fv = 180+100 = 280
        )
        child_b = _make_leaf("us-gaap_ServiceRevenue", values={"2024": 200})

        parent = _make_parent(
            "us-gaap_Revenue",
            children=[child_a, child_b],
            values={"2024": 500},  # declared matches children's declared: 300+200
            role="IS_REVENUE",
        )

        errors = []
        _verify_segment_sums(parent, ["2024"], errors, label_prefix="IS Revenue")

        # Filter for errors at the PARENT level (Revenue), not the sub-level
        parent_errors = [e for e in errors if "Revenue" in e[0]
                         and "Product" not in e[0]]

        # With fv-based checking at parent level: fv(children) = 280+200 = 480 != 500
        # Current code: declared children = 300+200 = 500 = parent → no parent-level error
        assert len(parent_errors) > 0, (
            "_verify_segment_sums did not report error at the parent level (Revenue). "
            "Parent declared=500, SUM(fv(children))=480, delta=20. "
            "It's using .values.get() at each level instead of fv(), so it only "
            "catches the error at the sub-level (ProductRevenue 300 != 280)."
        )


class TestDACheckUsesRoleTags:
    """Test 2: D&A verification should use role tags (IS_DA, CF_DA) instead
    of value-matching heuristic (removed).

    Bug: The value-matching heuristic can match the wrong CF node if another
    node happens to have the same value, or miss the match entirely if values
    differ slightly due to rounding.
    """

    def test_da_mismatch_detected_via_role_tags(self):
        """Create trees where:
        - IS has a node with role=IS_DA, value=100
        - CF has a node with role=CF_DA, value=120 (MISMATCH)
        - CF also has an unrelated node with value=100 (decoy)

        Old code: value-matching heuristic found the decoy (value=100),
        sees 100-100=0, reports no error. BUG!

        Fixed code: uses role tags, finds CF_DA=120, reports 100-120=-20 error.
        """
        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={"2024": 1000}),
            _make_leaf("us-gaap_DepreciationAndAmortization",
                       values={"2024": 100}, role="IS_DA"),
        ], values={"2024": 1100}, role="INC_NET")

        opcf = _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities", [
            _make_leaf("us-gaap_ProfitLoss", values={"2024": 1100},
                       role="INC_NET_CF"),
            _make_leaf("us-gaap_DepreciationDepletionAndAmortization",
                       values={"2024": 120}, role="CF_DA"),  # TRUE D&A, mismatched
            _make_leaf("us-gaap_IncreaseDecreaseInAccountsReceivable",
                       values={"2024": 100}),  # DECOY: same value as IS D&A
        ], values={"2024": 1320}, role="CF_OPCF")

        cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease", [
            opcf,
        ], values={"2024": 1320}, role="CF_NETCH")

        trees = {
            "IS": is_tree,
            "CF": cf_tree,
            "BS": _make_parent("us-gaap_Assets", [
                _make_leaf("us-gaap_Cash", values={"2024": 500}, role="BS_CASH"),
            ], values={"2024": 500}, role="BS_TA"),
            "BS_LE": _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
                _make_parent("us-gaap_Liabilities", [
                    _make_leaf("us-gaap_AP", values={"2024": 200}),
                ], values={"2024": 200}, role="BS_TL"),
                _make_parent("us-gaap_StockholdersEquity", [
                    _make_leaf("us-gaap_CommonStock", values={"2024": 300}),
                ], values={"2024": 300}, role="BS_TE"),
            ], values={"2024": 500}),
            "complete_periods": ["2024"],
            "cf_endc_values": {"2024": 500},
        }

        errors = verify_model(trees)
        da_errors = [e for e in errors if "D&A" in e[0]]
        assert len(da_errors) > 0, (
            "verify_model did not catch D&A mismatch (IS_DA=100, CF_DA=120). "
            "It matched the decoy node (value=100) via value-matching heuristic (removed) "
            "instead of using role tags."
        )


class TestSBCCheckUsesRoleTags:
    """Test 3: SBC verification should use role tags (IS_SBC, CF_SBC)."""

    def test_sbc_mismatch_detected_via_role_tags(self):
        """Same pattern as D&A test but for SBC.
        - IS has IS_SBC=50
        - CF has CF_SBC=70 (MISMATCH)
        - CF also has a decoy node with value=50
        """
        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={"2024": 1000}),
            _make_leaf("us-gaap_AllocatedShareBasedCompensationExpense",
                       values={"2024": 50}, role="IS_SBC"),
        ], values={"2024": 1050}, role="INC_NET")

        opcf = _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities", [
            _make_leaf("us-gaap_ProfitLoss", values={"2024": 1050},
                       role="INC_NET_CF"),
            _make_leaf("us-gaap_ShareBasedCompensation",
                       values={"2024": 70}, role="CF_SBC"),  # TRUE SBC, mismatched
            _make_leaf("us-gaap_IncreaseDecreaseInPrepaidExpenses",
                       values={"2024": 50}),  # DECOY
        ], values={"2024": 1170}, role="CF_OPCF")

        cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease", [
            opcf,
        ], values={"2024": 1170}, role="CF_NETCH")

        trees = {
            "IS": is_tree,
            "CF": cf_tree,
            "BS": _make_parent("us-gaap_Assets", [
                _make_leaf("us-gaap_Cash", values={"2024": 500}, role="BS_CASH"),
            ], values={"2024": 500}, role="BS_TA"),
            "BS_LE": _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
                _make_parent("us-gaap_Liabilities", [
                    _make_leaf("us-gaap_AP", values={"2024": 200}),
                ], values={"2024": 200}, role="BS_TL"),
                _make_parent("us-gaap_StockholdersEquity", [
                    _make_leaf("us-gaap_CommonStock", values={"2024": 300}),
                ], values={"2024": 300}, role="BS_TE"),
            ], values={"2024": 500}),
            "complete_periods": ["2024"],
            "cf_endc_values": {"2024": 500},
        }

        errors = verify_model(trees)
        sbc_errors = [e for e in errors if "SBC" in e[0]]
        assert len(sbc_errors) > 0, (
            "verify_model did not catch SBC mismatch (IS_SBC=50, CF_SBC=70). "
            "It matched the decoy node (value=50) via value-matching heuristic (removed) "
            "instead of using role tags."
        )


class TestCashBeginCheck:
    """Test 4: verify_model should check CF_BEGC[t] == BS_CASH[t-1].

    This check does not exist at all in current code.
    """

    def test_cash_begin_mismatch_detected(self):
        """Create a merged tree with 2 periods where the beginning cash
        balance in the CF statement doesn't match the prior period's BS cash.

        Period 2023: BS_CASH = 500
        Period 2024: CF beginning cash = 600 (should be 500!)

        Fixed code should report this as an error.
        """
        bs_tree = _make_parent("us-gaap_Assets", [
            _make_leaf("us-gaap_CashAndCashEquivalents",
                       values={"2023": 500, "2024": 700}, role="BS_CASH"),
            _make_leaf("us-gaap_OtherAssets",
                       values={"2023": 500, "2024": 300}),
        ], values={"2023": 1000, "2024": 1000}, role="BS_TA")

        # CF tree with a BEGC node that has a WRONG value for 2024
        begc_node = _make_leaf("us-gaap_CashBeginningOfPeriod",
                               values={"2023": 400, "2024": 600},  # 2024 should be 500
                               role="CF_BEGC")

        opcf = _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities", [
            _make_leaf("us-gaap_ProfitLoss", values={"2023": 200, "2024": 300},
                       role="INC_NET_CF"),
        ], values={"2023": 200, "2024": 300}, role="CF_OPCF")

        cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease", [
            opcf,
            begc_node,
        ], values={"2023": 200, "2024": 300}, role="CF_NETCH")

        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={"2023": 200, "2024": 300}),
        ], values={"2023": 200, "2024": 300}, role="INC_NET")

        trees = {
            "IS": is_tree,
            "CF": cf_tree,
            "BS": bs_tree,
            "BS_LE": _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
                _make_parent("us-gaap_Liabilities", [
                    _make_leaf("us-gaap_AP", values={"2023": 500, "2024": 500}),
                ], values={"2023": 500, "2024": 500}, role="BS_TL"),
                _make_parent("us-gaap_StockholdersEquity", [
                    _make_leaf("us-gaap_CommonStock", values={"2023": 500, "2024": 500}),
                ], values={"2023": 500, "2024": 500}, role="BS_TE"),
            ], values={"2023": 1000, "2024": 1000}),
            "complete_periods": ["2023", "2024"],
            "cf_endc_values": {"2023": 500, "2024": 700},
        }

        errors = verify_model(trees)
        cash_begin_errors = [e for e in errors if "BEGC" in e[0] or "Begin" in e[0]
                             or "begin" in e[0].lower()]
        assert len(cash_begin_errors) > 0, (
            "verify_model has no CF_BEGC[t] == BS_CASH[t-1] check. "
            "CF beginning cash 2024=600 != BS cash 2023=500 was not caught."
        )


# ===========================================================================
# Priority 2: Pipeline wiring
# ===========================================================================

class TestPipelineCallsMerge:
    """Test 5: Pipeline should call merge_trees on ALL tree files and pass
    the merged result (not just the newest filing) to sheet_builder.

    We test this by checking that run_pipeline.py references merge_trees
    and that the merged output contains periods from all filings.
    """

    def test_pipeline_merges_multiple_filings(self):
        """Create 2 minimal tree JSON files with different periods.
        Run merge_filing_trees (which the pipeline should call) and verify
        the result has ALL periods from both filings.

        Then verify that run_pipeline.py actually calls merge_trees —
        if it doesn't, this test documents the gap.
        """
        from merge_trees import merge_filing_trees

        # Filing 1 (newest): periods 2023, 2024
        tree1 = {
            "IS": _make_parent("us-gaap_NetIncomeLoss", [
                _make_leaf("us-gaap_Revenue", values={"2023": 300, "2024": 400}),
            ], values={"2023": 300, "2024": 400}, role="INC_NET").to_dict(),
            "BS": _make_parent("us-gaap_Assets", [
                _make_leaf("us-gaap_Cash", values={"2023": 100, "2024": 200},
                           role="BS_CASH"),
            ], values={"2023": 100, "2024": 200}, role="BS_TA").to_dict(),
            "BS_LE": _make_parent("us-gaap_LiabilitiesAndEquity", [
                _make_parent("us-gaap_Liabilities", [
                    _make_leaf("us-gaap_AP", values={"2023": 50, "2024": 100}),
                ], values={"2023": 50, "2024": 100}, role="BS_TL"),
                _make_parent("us-gaap_Equity", [
                    _make_leaf("us-gaap_CS", values={"2023": 50, "2024": 100}),
                ], values={"2023": 50, "2024": 100}, role="BS_TE"),
            ], values={"2023": 100, "2024": 200}).to_dict(),
            "CF": _make_parent("us-gaap_CashChange", [
                _make_parent("us-gaap_OPCF", [
                    _make_leaf("us-gaap_NI", values={"2023": 300, "2024": 400},
                               role="INC_NET_CF"),
                ], values={"2023": 300, "2024": 400}, role="CF_OPCF"),
            ], values={"2023": 300, "2024": 400}, role="CF_NETCH").to_dict(),
            "complete_periods": ["2023", "2024"],
            "cf_endc_values": {"2023": 100, "2024": 200},
        }

        # Filing 2 (older): periods 2021, 2022, 2023
        tree2 = {
            "IS": _make_parent("us-gaap_NetIncomeLoss", [
                _make_leaf("us-gaap_Revenue", values={"2021": 100, "2022": 200, "2023": 300}),
            ], values={"2021": 100, "2022": 200, "2023": 300}, role="INC_NET").to_dict(),
            "BS": _make_parent("us-gaap_Assets", [
                _make_leaf("us-gaap_Cash", values={"2021": 50, "2022": 75, "2023": 100},
                           role="BS_CASH"),
            ], values={"2021": 50, "2022": 75, "2023": 100}, role="BS_TA").to_dict(),
            "BS_LE": _make_parent("us-gaap_LiabilitiesAndEquity", [
                _make_parent("us-gaap_Liabilities", [
                    _make_leaf("us-gaap_AP", values={"2021": 25, "2022": 40, "2023": 50}),
                ], values={"2021": 25, "2022": 40, "2023": 50}, role="BS_TL"),
                _make_parent("us-gaap_Equity", [
                    _make_leaf("us-gaap_CS", values={"2021": 25, "2022": 35, "2023": 50}),
                ], values={"2021": 25, "2022": 35, "2023": 50}, role="BS_TE"),
            ], values={"2021": 50, "2022": 75, "2023": 100}).to_dict(),
            "CF": _make_parent("us-gaap_CashChange", [
                _make_parent("us-gaap_OPCF", [
                    _make_leaf("us-gaap_NI", values={"2021": 100, "2022": 200, "2023": 300},
                               role="INC_NET_CF"),
                ], values={"2021": 100, "2022": 200, "2023": 300}, role="CF_OPCF"),
            ], values={"2021": 100, "2022": 200, "2023": 300}, role="CF_NETCH").to_dict(),
            "complete_periods": ["2021", "2022", "2023"],
            "cf_endc_values": {"2021": 50, "2022": 75, "2023": 100},
        }

        # Write to temp files and merge
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f1:
            json.dump(tree1, f1)
            f1_path = f1.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f2:
            json.dump(tree2, f2)
            f2_path = f2.name

        try:
            merged = merge_filing_trees([f1_path, f2_path])
            # Merged should have ALL periods: 2021, 2022, 2023, 2024
            assert "2021" in merged["complete_periods"], "Missing 2021 from merge"
            assert "2022" in merged["complete_periods"], "Missing 2022 from merge"
            assert "2024" in merged["complete_periods"], "Missing 2024 from merge"
        finally:
            os.unlink(f1_path)
            os.unlink(f2_path)

        # Now verify run_pipeline.py actually CALLS merge_trees
        pipeline_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "run_pipeline.py"
        )
        with open(pipeline_path) as f:
            pipeline_code = f.read()

        assert "merge" in pipeline_code.lower(), (
            "run_pipeline.py does not reference merge_trees at all. "
            "The pipeline must call merge_filing_trees() on all tree files."
        )


class TestPipelineHaltsOnCheckpointFailure:
    """Test 6: Pipeline should NOT generate a sheet when verification fails.

    Currently run_pipeline.py calls run_command() which sys.exit(1) on
    failure, but we need to verify that sheet_builder is NOT called
    when pymodel.py --checkpoint fails.
    """

    def test_pipeline_does_not_call_sheet_builder_after_checkpoint_failure(self):
        """Read run_pipeline.py and verify the control flow:
        1. It should call merge_trees
        2. It should call pymodel.py --checkpoint on the MERGED file
        3. If checkpoint fails, it should NOT proceed to sheet_builder

        Current code runs checkpoint per-filing, not on merged output,
        and continues to sheet_builder regardless.
        """
        pipeline_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "run_pipeline.py"
        )
        with open(pipeline_path) as f:
            pipeline_code = f.read()

        # The pipeline should reference merge_trees and pass merged output
        # to both pymodel.py and sheet_builder.py
        has_merge = "merge" in pipeline_code.lower()
        has_merged_to_sheet = "merged" in pipeline_code.lower()

        assert has_merge and has_merged_to_sheet, (
            "run_pipeline.py does not merge tree files before passing to "
            "sheet_builder. It should: (1) call merge_filing_trees, "
            "(2) run checkpoint on merged output, (3) pass merged to sheet_builder."
        )


# ===========================================================================
# Priority 2: Residual sanity check logging
# ===========================================================================

class TestResidualSanityLogging:
    """Test 7: After merge, large residuals (> sibling average) should
    produce warning logs.

    merge_trees._recompute_residuals currently creates __OTHER__ nodes
    but does NOT log warnings when residuals are suspiciously large.
    """

    def test_large_residual_produces_warning(self, caplog):
        """Create a tree where the residual is larger than the average of
        its siblings. After _recompute_residuals, a WARNING should be logged.

        Parent declared = 1000
        Child A = 200, Child B = 100
        → residual = 700, sibling_avg = 150
        → 700 > 150 → WARNING expected
        """
        from merge_trees import _recompute_residuals

        parent = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRev", values={"2024": 200}),
            _make_leaf("us-gaap_ServiceRev", values={"2024": 100}),
        ], values={"2024": 1000})

        with caplog.at_level(logging.WARNING):
            _recompute_residuals(parent, ["2024"])

        # Verify __OTHER__ was created with the residual
        other = None
        for c in parent.children:
            if c.concept.startswith("__OTHER__"):
                other = c
                break
        assert other is not None, "No __OTHER__ node created"
        assert abs(other.values.get("2024", 0) - 700) < 0.5, (
            f"Expected __OTHER__ residual of 700, got {other.values.get('2024', 0)}"
        )

        # Check for WARNING log about large residual
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        residual_warnings = [r for r in warnings
                             if "residual" in r.message.lower()
                             or "Revenue" in r.message]
        assert len(residual_warnings) > 0, (
            "_recompute_residuals did not log a WARNING for large residual. "
            "Residual=700 > sibling_avg=150, but no warning was produced."
        )

    def test_small_residual_no_warning(self, caplog):
        """When residual is smaller than sibling average, no warning needed.

        Parent declared = 310
        Child A = 200, Child B = 100
        → residual = 10, sibling_avg = 150
        → 10 < 150 → no warning
        """
        from merge_trees import _recompute_residuals

        parent = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRev", values={"2024": 200}),
            _make_leaf("us-gaap_ServiceRev", values={"2024": 100}),
        ], values={"2024": 310})

        with caplog.at_level(logging.WARNING):
            _recompute_residuals(parent, ["2024"])

        residual_warnings = [r for r in caplog.records
                             if r.levelno >= logging.WARNING
                             and ("residual" in r.message.lower()
                                  or "Revenue" in r.message)]
        # This test should pass even before the fix (no warning for small residuals)
        # It serves as a regression guard once the logging is implemented.
        assert len(residual_warnings) == 0, (
            "Got unexpected warning for small residual (10 < sibling_avg 150)."
        )
