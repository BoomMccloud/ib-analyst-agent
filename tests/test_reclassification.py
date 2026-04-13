"""
TDD Integration Tests: Reclassification Detection
===================================================
Tests for _detect_and_fix_structural_shifts() in merge_trees.py.

Covers:
- Pattern 1: Parent-Child Promotion (the TSLA revenue case)
- Pattern 2: Sibling Replacement
- Negative cases (OTHER nodes, unrelated nodes, no newer periods)
- TSLA re-merge integration (fixture-based)

All tests should FAIL before implementation — they define "done".
"""

import glob
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode
from concept_matcher import ConceptMatcher

def _detect_and_fix_structural_shifts(tree, periods):
    return ConceptMatcher().detect_and_fix_structural_shifts(tree, periods)


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


def _find_by_concept(tree, concept):
    """Recursively find a node by concept in the tree."""
    if tree.concept == concept:
        return tree
    for c in tree.children:
        result = _find_by_concept(c, concept)
        if result:
            return result
    return None


def _all_concepts(tree):
    """Collect all concept names in the tree."""
    concepts = {tree.concept}
    for c in tree.children:
        concepts.update(_all_concepts(c))
    return concepts


# ===========================================================================
# Pattern 1: Parent-Child Promotion
# ===========================================================================

class TestParentChildPromotion:
    """Pattern 1: A parent node's value matches its child at an overlap period,
    and the child extends into newer periods. The child should replace the parent."""

    def test_child_inherits_parent_old_period_values(self):
        """After fix, the promoted child should have the parent's old-period
        values merged in, so no data is lost."""
        # Parent has old periods 2020, 2021, 2022
        # Child has overlap at 2022 (same value) and extends to 2023, 2024
        child = _make_leaf(
            "us-gaap_RevenueFromContractWithCustomer",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        other_child = _make_leaf(
            "us-gaap_ProductRevenue",
            values={"2022": 300, "2023": 350, "2024": 400},
        )
        parent = _make_parent(
            "us-gaap_Revenues",
            children=[child, other_child],
            values={"2020": 300, "2021": 400, "2022": 500},
        )
        root = _make_parent("us-gaap_NetIncomeLoss", children=[parent],
                            values={"2020": 300, "2021": 400, "2022": 500,
                                    "2023": 600, "2024": 700})

        periods = ["2020", "2021", "2022", "2023", "2024"]
        _detect_and_fix_structural_shifts(root, periods)

        # The promoted child should now have parent's old-period values
        promoted = _find_by_concept(root, "us-gaap_RevenueFromContractWithCustomer")
        assert promoted is not None, "Promoted child should still exist in tree"
        assert promoted.values.get("2020") == 300, "Should inherit parent 2020 value"
        assert promoted.values.get("2021") == 400, "Should inherit parent 2021 value"
        assert promoted.values.get("2022") == 500, "Overlap period unchanged"
        assert promoted.values.get("2023") == 600, "Newer period unchanged"
        assert promoted.values.get("2024") == 700, "Newer period unchanged"

    def test_parent_removed_from_tree(self):
        """After fix, the old parent node should no longer exist in the tree.
        The promoted child takes its position."""
        child = _make_leaf(
            "us-gaap_RevenueFromContractWithCustomer",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        parent = _make_parent(
            "us-gaap_Revenues",
            children=[child],
            values={"2020": 300, "2021": 400, "2022": 500},
        )
        root = _make_parent("us-gaap_NetIncomeLoss", children=[parent],
                            values={"2020": 300, "2021": 400, "2022": 500,
                                    "2023": 600, "2024": 700})

        periods = ["2020", "2021", "2022", "2023", "2024"]
        _detect_and_fix_structural_shifts(root, periods)

        # Old parent concept should be gone
        old_parent = _find_by_concept(root, "us-gaap_Revenues")
        assert old_parent is None, "Old parent should be removed from tree"

        # Promoted child should be a direct child of root
        assert any(
            c.concept == "us-gaap_RevenueFromContractWithCustomer"
            for c in root.children
        ), "Promoted child should occupy parent's position under root"

    def test_role_transferred_to_promoted_child(self):
        """If the old parent has a role (e.g. IS_REVENUE), the promoted child
        should inherit that role."""
        child = _make_leaf(
            "us-gaap_RevenueFromContractWithCustomer",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        parent = _make_parent(
            "us-gaap_Revenues",
            children=[child],
            values={"2020": 300, "2021": 400, "2022": 500},
            role="IS_REVENUE",
        )
        root = _make_parent("us-gaap_NetIncomeLoss", children=[parent],
                            values={"2020": 300, "2021": 400, "2022": 500,
                                    "2023": 600, "2024": 700},
                            role="INC_NET")

        periods = ["2020", "2021", "2022", "2023", "2024"]
        _detect_and_fix_structural_shifts(root, periods)

        promoted = _find_by_concept(root, "us-gaap_RevenueFromContractWithCustomer")
        assert promoted is not None
        assert promoted.role == "IS_REVENUE", \
            f"Role should transfer to promoted child, got {promoted.role!r}"

    def test_non_residual_children_adopted_by_promoted_child(self):
        """Non-residual siblings of the promoted child (under the old parent)
        should become children of the promoted child."""
        promoted_child = _make_leaf(
            "us-gaap_RevenueFromContractWithCustomer",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        sibling = _make_leaf(
            "us-gaap_ProductRevenue",
            values={"2022": 300, "2023": 350, "2024": 400},
        )
        parent = _make_parent(
            "us-gaap_Revenues",
            children=[promoted_child, sibling],
            values={"2020": 300, "2021": 400, "2022": 500},
            role="IS_REVENUE",
        )
        root = _make_parent("us-gaap_NetIncomeLoss", children=[parent],
                            values={"2020": 300, "2021": 400, "2022": 500,
                                    "2023": 600, "2024": 700})

        periods = ["2020", "2021", "2022", "2023", "2024"]
        _detect_and_fix_structural_shifts(root, periods)

        promoted = _find_by_concept(root, "us-gaap_RevenueFromContractWithCustomer")
        assert promoted is not None
        # The sibling should now be a child of the promoted node
        child_concepts = [c.concept for c in promoted.children]
        assert "us-gaap_ProductRevenue" in child_concepts, \
            f"Sibling should be adopted by promoted child, children are: {child_concepts}"


# ===========================================================================
# Pattern 2: Sibling Replacement
# ===========================================================================

class TestSiblingReplacement:
    """Pattern 2: Two siblings under the same parent share a value at an
    overlap period, and one extends to newer periods. The newer sibling
    should absorb the older one's values and replace it."""

    def test_newer_sibling_absorbs_older_values(self):
        """The newer sibling should get the older sibling's old-period values,
        and the older sibling should be removed."""
        old_sibling = _make_leaf(
            "us-gaap_Revenues",
            values={"2020": 300, "2021": 400, "2022": 500},
        )
        new_sibling = _make_leaf(
            "us-gaap_RevenueFromContractWithCustomer",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        parent = _make_parent(
            "us-gaap_NetIncomeLoss",
            children=[old_sibling, new_sibling],
            values={"2020": 300, "2021": 400, "2022": 500,
                    "2023": 600, "2024": 700},
            role="INC_NET",
        )

        periods = ["2020", "2021", "2022", "2023", "2024"]
        _detect_and_fix_structural_shifts(parent, periods)

        # Old sibling should be gone
        assert _find_by_concept(parent, "us-gaap_Revenues") is None, \
            "Old sibling should be removed"

        # New sibling should have old values
        new_node = _find_by_concept(parent, "us-gaap_RevenueFromContractWithCustomer")
        assert new_node is not None
        assert new_node.values.get("2020") == 300
        assert new_node.values.get("2021") == 400
        assert new_node.values.get("2022") == 500
        assert new_node.values.get("2023") == 600
        assert new_node.values.get("2024") == 700


# ===========================================================================
# Negative Cases
# ===========================================================================

class TestNegativeCases:
    """Cases where _detect_and_fix_structural_shifts should NOT make changes."""

    def test_other_nodes_not_detected(self):
        """__OTHER__ residual nodes should never be treated as structural
        shift candidates, even if their values match another node."""
        other_node = _make_leaf(
            "__OTHER___Revenues",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        real_node = _make_leaf(
            "us-gaap_Revenues",
            values={"2020": 300, "2021": 400, "2022": 500},
        )
        parent = _make_parent(
            "us-gaap_NetIncomeLoss",
            children=[real_node, other_node],
            values={"2020": 300, "2021": 400, "2022": 500,
                    "2023": 600, "2024": 700},
        )

        periods = ["2020", "2021", "2022", "2023", "2024"]
        concepts_before = _all_concepts(parent)
        _detect_and_fix_structural_shifts(parent, periods)
        concepts_after = _all_concepts(parent)

        assert concepts_before == concepts_after, \
            "No nodes should be removed when one is an __OTHER__ residual"

    def test_no_structural_relationship_no_match(self):
        """Nodes in different subtrees (no parent-child or shared-parent
        relationship) should NOT be matched, even if values overlap."""
        # Two subtrees under root, each with a node that has the same value
        subtree_a = _make_parent(
            "us-gaap_Revenue",
            children=[
                _make_leaf("us-gaap_ProductRevenue",
                           values={"2020": 300, "2021": 400, "2022": 500}),
            ],
            values={"2020": 300, "2021": 400, "2022": 500},
        )
        subtree_b = _make_parent(
            "us-gaap_OperatingExpenses",
            children=[
                _make_leaf("us-gaap_CostOfRevenue",
                           values={"2022": 500, "2023": 600, "2024": 700}),
            ],
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        root = _make_parent(
            "us-gaap_NetIncomeLoss",
            children=[subtree_a, subtree_b],
            values={"2020": 300, "2021": 400, "2022": 500,
                    "2023": 600, "2024": 700},
        )

        periods = ["2020", "2021", "2022", "2023", "2024"]
        concepts_before = _all_concepts(root)
        _detect_and_fix_structural_shifts(root, periods)
        concepts_after = _all_concepts(root)

        assert concepts_before == concepts_after, \
            "Nodes in different subtrees should not trigger reclassification"

    def test_same_periods_no_newer_extension(self):
        """If both nodes cover the exact same periods (no newer extension),
        there is no reclassification to detect."""
        node_a = _make_leaf(
            "us-gaap_Revenues",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        node_b = _make_leaf(
            "us-gaap_RevenueFromContractWithCustomer",
            values={"2022": 500, "2023": 600, "2024": 700},
        )
        parent = _make_parent(
            "us-gaap_NetIncomeLoss",
            children=[node_a, node_b],
            values={"2022": 500, "2023": 600, "2024": 700},
        )

        periods = ["2022", "2023", "2024"]
        concepts_before = _all_concepts(parent)
        _detect_and_fix_structural_shifts(parent, periods)
        concepts_after = _all_concepts(parent)

        assert concepts_before == concepts_after, \
            "Same-period nodes should not trigger reclassification"


# ===========================================================================
# Edge Cases
# ===========================================================================

class TestEdgeCases:
    """Unit tests for internal detection edge cases."""

    def test_zero_value_overlap_no_match(self):
        old = _make_leaf("us-gaap_Revenues",
                         values={"2020": 300, "2021": 400, "2022": 0})
        new = _make_leaf("us-gaap_RevenueFromContract",
                         values={"2022": 0, "2023": 600, "2024": 700})
        parent = _make_parent("Root", children=[old, new],
                              values={"2020": 300, "2021": 400, "2022": 0,
                                      "2023": 600, "2024": 700})
        periods = ["2020", "2021", "2022", "2023", "2024"]
        concepts_before = _all_concepts(parent)
        _detect_and_fix_structural_shifts(parent, periods)
        assert concepts_before == _all_concepts(parent)

    def test_multiple_overlap_periods_still_detects(self):
        old = _make_leaf("us-gaap_Revenues",
                         values={"2020": 300, "2021": 400, "2022": 500})
        new = _make_leaf("us-gaap_RevenueFromContract",
                         values={"2021": 400, "2022": 500, "2023": 600})
        parent = _make_parent("Root", children=[old, new],
                              values={"2020": 300, "2021": 400, "2022": 500,
                                      "2023": 600})
        periods = ["2020", "2021", "2022", "2023"]
        _detect_and_fix_structural_shifts(parent, periods)
        assert _find_by_concept(parent, "us-gaap_Revenues") is None
        promoted = _find_by_concept(parent, "us-gaap_RevenueFromContract")
        assert promoted is not None
        assert promoted.values.get("2020") == 300

    def test_child_role_not_overwritten(self):
        child = _make_leaf("us-gaap_RevenueFromContract",
                           values={"2022": 500, "2023": 600},
                           role="EXISTING_ROLE")
        parent = _make_parent("us-gaap_Revenues",
                              children=[child],
                              values={"2020": 300, "2022": 500},
                              role="IS_REVENUE")
        root = _make_parent("Root", children=[parent],
                            values={"2020": 300, "2022": 500, "2023": 600})
        periods = ["2020", "2022", "2023"]
        _detect_and_fix_structural_shifts(root, periods)
        promoted = _find_by_concept(root, "us-gaap_RevenueFromContract")
        assert promoted is not None
        assert promoted.role == "EXISTING_ROLE"

    def test_returns_stats_dict(self):
        child = _make_leaf("us-gaap_RevenueFromContract",
                           values={"2022": 500, "2023": 600})
        parent = _make_parent("us-gaap_Revenues",
                              children=[child],
                              values={"2020": 300, "2022": 500})
        root = _make_parent("Root", children=[parent],
                            values={"2020": 300, "2022": 500, "2023": 600})
        periods = ["2020", "2022", "2023"]
        stats = _detect_and_fix_structural_shifts(root, periods)
        assert isinstance(stats, dict)
        assert "detections" in stats
        assert "fixes_applied" in stats
        assert stats["fixes_applied"] == 1


# ===========================================================================
# Integration: TSLA Re-Merge
# ===========================================================================

TSLA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pipeline_output", "validation", "TSLA",
)


@pytest.mark.skipif(
    not glob.glob(os.path.join(TSLA_DIR, "trees_*.json")),
    reason="TSLA fixture data not found",
)
class TestTSLAReMerge:
    """End-to-end: merge actual TSLA filings and verify reclassification
    errors are eliminated."""

    def test_tsla_no_is_revenue_errors_after_merge(self):
        """After merging TSLA filings with reclassification detection,
        there should be no IS Revenue errors in verify_model output."""
        from merge_trees import merge_filing_trees
        from pymodel import verify_model

        tree_files = sorted(glob.glob(os.path.join(TSLA_DIR, "trees_*.json")))
        # merge_filing_trees expects newest first
        tree_files = list(reversed(tree_files))

        merged = merge_filing_trees(tree_files)
        errors = verify_model(merged)
        revenue_errors = [e for e in errors if "Revenue" in e[0] or "IS_REVENUE" in e[0]]
        assert len(revenue_errors) == 0, \
            f"Should have no IS Revenue errors after reclassification fix, got: {revenue_errors}"

    def test_tsla_ni_link_errors_under_threshold(self):
        """After merging TSLA filings, NI Link errors should be < 150
        (NCI noise), not the 96K caused by reclassification."""
        from merge_trees import merge_filing_trees
        from pymodel import verify_model

        tree_files = sorted(glob.glob(os.path.join(TSLA_DIR, "trees_*.json")))
        tree_files = list(reversed(tree_files))

        merged = merge_filing_trees(tree_files)
        errors = verify_model(merged)
        ni_errors = [e for e in errors if "NI Link" in e[0] or "INC_NET" in e[0]]
        for err in ni_errors:
            delta = abs(err[2])
            if delta > 150:
                pytest.fail(f"NI Link error exceeds 150 threshold: {err}")
