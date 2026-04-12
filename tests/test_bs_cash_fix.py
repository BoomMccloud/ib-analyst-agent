"""
BS_CASH Fix + Matching Cleanup — Integration Tests
====================================================
Tests for:
1. BS_CASH concept-name matching (not position-based)
2. BS_CASH position fallback when no cash concept found
3. BS_CASH namespace prefix stripping
4. Unified _find_by_keywords: DFS all-match behavior
5. Unified _find_by_keywords: BFS any-match behavior

These tests should FAIL before the fix is applied.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, _tag_bs_positions, find_node_by_role


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


PERIODS = ["2022", "2023", "2024"]


# ===========================================================================
# 1. BS_CASH concept-name match
# ===========================================================================

class TestBSCashConceptMatch:
    """BS_CASH should be assigned by concept name, not by position."""

    def test_cash_tagged_by_concept_not_position(self):
        """When children are [PrepaidExpense, CashAndCashEquivalents],
        BS_CASH must land on the cash node (position 1), not position 0.

        This fails with the current bug because _tag_bs_positions does
        child.children[0].role = "BS_CASH" unconditionally.
        """
        # TCA subtree: cash is the SECOND child, not first
        prepaid = _make_leaf(
            "us-gaap_PrepaidExpenseAndOtherAssetsCurrent",
            values={"2024": 200},
        )
        cash = _make_leaf(
            "us-gaap_CashAndCashEquivalentsAtCarryingValue",
            values={"2024": 5000},
        )
        tca = _make_parent(
            "us-gaap_AssetsCurrent",
            [prepaid, cash],
            values={"2024": 5200},
        )

        # Assets root wrapping TCA
        assets = _make_parent(
            "us-gaap_Assets",
            [tca],
            values={"2024": 10000},
        )

        _tag_bs_positions(assets, None)

        # The cash node should have BS_CASH, not the prepaid node
        assert cash.role == "BS_CASH", (
            f"Expected cash node to be BS_CASH, but got role={cash.role}; "
            f"prepaid node role={prepaid.role}"
        )
        assert prepaid.role != "BS_CASH", (
            "PrepaidExpense was incorrectly tagged as BS_CASH"
        )


# ===========================================================================
# 2. BS_CASH position fallback
# ===========================================================================

class TestBSCashPositionFallback:
    """When no child has a cash-related concept, BS_CASH falls back to children[0]."""

    def test_fallback_to_first_child_when_no_cash_concept(self):
        """TCA with no cash-related concept should still tag children[0] as BS_CASH."""
        inventory = _make_leaf(
            "us-gaap_InventoryNet",
            values={"2024": 800},
        )
        receivables = _make_leaf(
            "us-gaap_AccountsReceivableNet",
            values={"2024": 1200},
        )
        tca = _make_parent(
            "us-gaap_AssetsCurrent",
            [inventory, receivables],
            values={"2024": 2000},
        )
        assets = _make_parent(
            "us-gaap_Assets",
            [tca],
            values={"2024": 5000},
        )

        _tag_bs_positions(assets, None)

        # Fallback: first child gets BS_CASH
        assert inventory.role == "BS_CASH", (
            f"Expected fallback to children[0], got role={inventory.role}"
        )


# ===========================================================================
# 3. BS_CASH with namespace prefix
# ===========================================================================

class TestBSCashNamespacePrefix:
    """BS_CASH matching should work even when concept has a namespace prefix."""

    def test_namespace_prefix_stripped_for_match(self):
        """Concept like 'us-gaap:CashAndCashEquivalentsAtCarryingValue'
        should still match after stripping the namespace prefix."""
        other = _make_leaf(
            "us-gaap_PrepaidExpenseAndOtherAssetsCurrent",
            values={"2024": 300},
        )
        # Note: concept uses colon separator (namespace prefix style)
        cash = _make_leaf(
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
            values={"2024": 4000},
        )
        tca = _make_parent(
            "us-gaap_AssetsCurrent",
            [other, cash],
            values={"2024": 4300},
        )
        assets = _make_parent(
            "us-gaap_Assets",
            [tca],
            values={"2024": 9000},
        )

        _tag_bs_positions(assets, None)

        assert cash.role == "BS_CASH", (
            f"Namespace-prefixed cash concept not matched: role={cash.role}"
        )
        assert other.role != "BS_CASH", (
            "Non-cash node incorrectly tagged as BS_CASH"
        )


# ===========================================================================
# 4. Unified keyword search — DFS all-match
# ===========================================================================

class TestUnifiedKeywordSearchDFSAll:
    """Unified _find_by_keywords with mode='all', search='dfs', leaf_only=True, field='name'
    should match only leaf nodes where ALL keywords appear in .name."""

    def test_dfs_all_match_leaf_only(self):
        """Only leaves whose .name contains ALL keywords should match."""
        from xbrl_tree import _find_by_keywords

        # Build a tree with various leaves
        leaf_da = _make_leaf("us-gaap_DepreciationAndAmortization",
                             values={"2024": 100})
        leaf_depreciation_only = _make_leaf("us-gaap_Depreciation",
                                            values={"2024": 50})
        leaf_amortization_only = _make_leaf("us-gaap_AmortizationOfIntangibles",
                                            values={"2024": 30})
        parent = _make_parent("us-gaap_OperatingExpenses",
                              [leaf_da, leaf_depreciation_only, leaf_amortization_only],
                              values={"2024": 180})
        root = _make_parent("us-gaap_NetIncomeLoss", [parent],
                            values={"2024": 500})

        # Search for nodes with BOTH "depreciation" AND "amortization" in .name
        result = _find_by_keywords(
            root,
            ["depreciation", "amortization"],
            mode="all",
            search="dfs",
            leaf_only=True,
            field="name",
        )

        assert result is not None, "Should find the D&A leaf"
        assert result.concept == "us-gaap_DepreciationAndAmortization"

    def test_dfs_all_match_rejects_partial(self):
        """A leaf with only ONE of two keywords should NOT match in mode='all'."""
        from xbrl_tree import _find_by_keywords

        leaf = _make_leaf("us-gaap_Depreciation", values={"2024": 50})
        root = _make_parent("us-gaap_Root", [leaf], values={"2024": 50})

        result = _find_by_keywords(
            root,
            ["depreciation", "amortization"],
            mode="all",
            search="dfs",
            leaf_only=True,
            field="name",
        )

        assert result is None, "Partial keyword match should not return a result"

    def test_dfs_all_match_skips_non_leaf(self):
        """Non-leaf nodes should be skipped when leaf_only=True."""
        from xbrl_tree import _find_by_keywords

        # Parent has both keywords in name but is not a leaf
        inner_leaf = _make_leaf("us-gaap_SomeChild", values={"2024": 10})
        parent_with_keywords = _make_parent(
            "us-gaap_DepreciationAndAmortization",
            [inner_leaf],
            values={"2024": 10},
        )
        root = _make_parent("us-gaap_Root", [parent_with_keywords],
                            values={"2024": 10})

        result = _find_by_keywords(
            root,
            ["depreciation", "amortization"],
            mode="all",
            search="dfs",
            leaf_only=True,
            field="name",
        )

        assert result is None, "Non-leaf should be skipped with leaf_only=True"


# ===========================================================================
# 5. Unified keyword search — BFS any-match
# ===========================================================================

class TestUnifiedKeywordSearchBFSAny:
    """Unified _find_by_keywords with mode='any', search='bfs', leaf_only=False, field='concept'
    should match the shallowest node where ANY keyword is in .concept."""

    def test_bfs_any_match_returns_shallowest(self):
        """BFS should return the shallowest node matching any keyword."""
        from xbrl_tree import _find_by_keywords

        # Deep node matches
        deep_match = _make_leaf("us-gaap_CostOfRevenue", values={"2024": 400})
        # Shallow node also matches
        shallow_match = _make_parent(
            "us-gaap_CostOfGoodsAndServicesSold",
            [deep_match],
            values={"2024": 400},
        )
        root = _make_parent("us-gaap_NetIncomeLoss", [shallow_match],
                            values={"2024": 200})

        result = _find_by_keywords(
            root,
            ["costofgoods", "costofrevenue"],
            mode="any",
            search="bfs",
            leaf_only=False,
            field="concept",
        )

        assert result is not None, "Should find a match"
        # BFS: shallowest match wins — the parent node at depth 1
        assert result.concept == "us-gaap_CostOfGoodsAndServicesSold", (
            f"Expected shallowest match, got {result.concept}"
        )

    def test_bfs_any_match_single_keyword_suffices(self):
        """In mode='any', a single keyword match is sufficient."""
        from xbrl_tree import _find_by_keywords

        leaf = _make_leaf("us-gaap_RevenueFromContractWithCustomer",
                          values={"2024": 1000})
        root = _make_parent("us-gaap_NetIncomeLoss", [leaf],
                            values={"2024": 200})

        result = _find_by_keywords(
            root,
            ["revenue", "sales"],
            mode="any",
            search="bfs",
            leaf_only=False,
            field="concept",
        )

        assert result is not None, "Single keyword should match in mode='any'"
        assert result.concept == "us-gaap_RevenueFromContractWithCustomer"

    def test_bfs_any_match_includes_non_leaf(self):
        """With leaf_only=False, non-leaf nodes should also be matchable."""
        from xbrl_tree import _find_by_keywords

        inner = _make_leaf("us-gaap_SomeChild", values={"2024": 10})
        non_leaf = _make_parent("us-gaap_Revenue", [inner],
                                values={"2024": 10})
        root = _make_parent("us-gaap_Root", [non_leaf], values={"2024": 10})

        result = _find_by_keywords(
            root,
            ["revenue"],
            mode="any",
            search="bfs",
            leaf_only=False,
            field="concept",
        )

        assert result is not None
        assert result.concept == "us-gaap_Revenue"
        assert not result.is_leaf, "The matched node is a parent, not a leaf"


# ===========================================================================
# 6. Unit tests specified by implementation guide
# ===========================================================================

class TestFindByKeywordsUnitTests:
    """Unit tests UT-1 and UT-2 from the implementation guide."""

    def test_ut1_no_match_returns_none(self):
        """UT-1: Single leaf with no matching keywords returns None."""
        from xbrl_tree import _find_by_keywords

        leaf = _make_leaf("us-gaap_Revenue", values={"2024": 100})
        result = _find_by_keywords(leaf, ["cash"], mode="all", search="dfs",
                                   leaf_only=True, field="name")
        assert result is None

    def test_ut2_field_concept_vs_name(self):
        """UT-2: field='concept' searches raw concept; field='name' searches cleaned name.
        'us-gaap' appears in .concept but not in .name."""
        from xbrl_tree import _find_by_keywords

        leaf = _make_leaf("us-gaap_CostOfGoodsSold", values={"2024": 100})
        root = _make_parent("us-gaap_Root", [leaf], values={"2024": 100})

        by_concept = _find_by_keywords(root, ["us-gaap"], mode="any", search="dfs",
                                       leaf_only=True, field="concept")
        assert by_concept is not None, "Should find 'us-gaap' in concept field"

        by_name = _find_by_keywords(root, ["us-gaap"], mode="any", search="dfs",
                                    leaf_only=True, field="name")
        assert by_name is None, "'us-gaap' should not appear in cleaned .name"
