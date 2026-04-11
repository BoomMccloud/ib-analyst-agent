"""
Phase 3 Tests: Sheet formula generation and two-pass rendering
==============================================================
Tests _build_weight_formula(), _render_sheet_body() (two-pass),
Check row formulas, and CF cash proof rows.
No Google Sheets API — tests formula strings and row structure.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheet_builder import _build_weight_formula, dcol


# ---------------------------------------------------------------------------
# _build_weight_formula tests
# ---------------------------------------------------------------------------

class TestBuildWeightFormula:
    def test_all_positive_contiguous(self):
        """3 contiguous +1 children → =SUM(E5:E7)"""
        result = _build_weight_formula("E", [(5, 1.0), (6, 1.0), (7, 1.0)])
        assert result == "=SUM(E5:E7)"
    
    def test_all_positive_noncontiguous(self):
        """2 non-contiguous +1 children → =E5+E8"""
        result = _build_weight_formula("E", [(5, 1.0), (8, 1.0)])
        assert result == "=E5+E8"
    
    def test_mixed_weights(self):
        """+1 and -1 children → =E4-E7"""
        result = _build_weight_formula("E", [(4, 1.0), (7, -1.0)])
        assert result == "=E4-E7"
    
    def test_single_child(self):
        """Single +1 child → =E5"""
        result = _build_weight_formula("E", [(5, 1.0)])
        assert result == "=E5"
    
    def test_single_child_negative(self):
        """Single -1 child → =-E5"""
        result = _build_weight_formula("E", [(5, -1.0)])
        assert result == "=-E5"
    
    def test_empty(self):
        """No children → empty string"""
        result = _build_weight_formula("E", [])
        assert result == ""
    
    def test_column_f(self):
        """Works with different column letters."""
        result = _build_weight_formula("F", [(5, 1.0), (6, 1.0)])
        assert result == "=SUM(F5:F6)"
    
    def test_three_mixed(self):
        """Three children: +1, +1, -1 → =E4+E5-E7"""
        result = _build_weight_formula("E", [(4, 1.0), (5, 1.0), (7, -1.0)])
        assert result == "=E4+E5-E7"


from xbrl_tree import TreeNode, reconcile_trees, find_node_by_role
from sheet_builder import _render_sheet_body


# ---------------------------------------------------------------------------
# Fixture helper (reuse from test_model_historical.py pattern)
# ---------------------------------------------------------------------------

def _make_leaf(concept, weight=1.0, values=None):
    node = TreeNode(concept, weight)
    node.values = values or {}
    return node


def _make_parent(concept, children, weight=1.0, values=None):
    node = TreeNode(concept, weight)
    node.values = values or {}
    for c in children:
        node.add_child(c)
    return node


PERIODS = ["2022", "2023", "2024"]


def _build_simple_tree():
    """Build a simple tree for rendering tests.
    
    Revenue (parent, weight=+1)
        Products (leaf, weight=+1)
        Services (leaf, weight=+1)
    """
    return _make_parent("us-gaap_Revenue", [
        _make_leaf("us-gaap_ProductRevenue", weight=1.0,
                   values={"2022": 300, "2023": 350, "2024": 400}),
        _make_leaf("us-gaap_ServiceRevenue", weight=1.0,
                   values={"2022": 100, "2023": 110, "2024": 120}),
    ], values={"2022": 400, "2023": 460, "2024": 520})


# ---------------------------------------------------------------------------
# Two-pass rendering tests
# ---------------------------------------------------------------------------

class TestTwoPassRendering:
    def test_leaf_cells_are_numbers(self):
        """Leaf rows must contain numbers (int/float), not formulas."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        # Rows 1 and 2 are children (leaves)
        for leaf_row in rows[1:]:  # skip parent row
            for cell in leaf_row[4:]:  # skip prefix columns
                assert not isinstance(cell, str) or not cell.startswith("="), \
                    f"Leaf cell should be a number, got: {cell}"
    
    def test_parent_cells_are_formulas(self):
        """Parent rows must contain formula strings starting with '='."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        parent_row = rows[0]  # first row = parent
        for cell in parent_row[4:]:  # data columns
            assert isinstance(cell, str) and cell.startswith("="), \
                f"Parent cell should be a formula, got: {cell}"
    
    def test_row_count_matches_tree_nodes(self):
        """Total rows = total nodes in tree (1 parent + 2 leaves = 3)."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert len(rows) == 3
    
    def test_two_pass_row_order_matches_tree_order(self):
        """Rows appear parent-first, then children depth-first."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        labels = [row[2].strip() for row in rows]
        assert labels[0] == "Revenue"
        assert labels[1] == "Product Revenue"
        assert labels[2] == "Service Revenue"
    
    def test_row_format_four_prefix_columns(self):
        """Every row starts with ["", "", label, ""]."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        for row in rows:
            assert row[0] == ""
            assert row[1] == ""
            assert isinstance(row[2], str) and len(row[2]) > 0
            assert row[3] == ""
    
    def test_indentation_matches_depth(self):
        """Depth-0 node has no indent, depth-1 nodes have 2-space indent."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert not rows[0][2].startswith(" "), "Root should have no indent"
        assert rows[1][2].startswith("  "), "Child should have 2-space indent"
        assert not rows[1][2].startswith("    "), "Child should have exactly 2-space indent"
    
    def test_role_map_populated(self):
        """Roles from the tree should appear in global_role_map."""
        tree = _build_simple_tree()
        tree.role = "IS_REVENUE"
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert "IS_REVENUE" in role_map
        assert role_map["IS_REVENUE"] == ("IS", 4)
    
    def test_parent_formula_references_correct_rows(self):
        """Parent formula should reference its children's actual row numbers."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        # Parent is row 4, children are rows 5 and 6
        # Children are contiguous +1, so formula should be =SUM(E5:E6)
        parent_formula = rows[0][4]  # first data column
        assert parent_formula == "=SUM(E5:E6)", f"Expected =SUM(E5:E6), got {parent_formula}"
    
    def test_data_columns_start_at_E(self):
        """Data values/formulas begin at index 4 (column E)."""
        tree = _build_simple_tree()
        role_map = {}
        rows = _render_sheet_body(tree, PERIODS, start_row=4, global_role_map=role_map,
                                   sheet_name="IS")
        assert len(rows[0]) == 4 + len(PERIODS)


# ---------------------------------------------------------------------------
# CF Cash Proof tests
# ---------------------------------------------------------------------------

class TestCFCashProof:
    def test_prev_period_middle(self):
        from sheet_builder import prev_period
        assert prev_period("2023", ["2021", "2022", "2023", "2024"]) == "2022"

    def test_prev_period_first(self):
        from sheet_builder import prev_period
        assert prev_period("2021", ["2021", "2022", "2023"]) is None

    def test_prev_period_last(self):
        from sheet_builder import prev_period
        assert prev_period("2024", ["2021", "2022", "2023", "2024"]) == "2023"
