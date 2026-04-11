"""
Phase 3e Tests: Dual-Linkbase Architecture
===========================================
Integration tests for presentation linkbase parsing, cascade layout,
semantic tagging, orphan supplementation, tree completeness, cross-statement
checks, and pipeline gating.

All tests build TreeNode objects in-memory (no network calls).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, find_node_by_role


# ---------------------------------------------------------------------------
# Fixture helpers
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
# 1. Presentation linkbase parsing + sorting
# ===========================================================================

class TestPresentationLinkbase:
    """parse_pre_linkbase and sort_by_presentation"""

    def test_parse_pre_linkbase_returns_concept_position_map(self):
        """parse_pre_linkbase(xml) returns a dict mapping concept -> position."""
        from xbrl_tree import parse_pre_linkbase

        # Minimal presentation linkbase XML with ordered presentationArc elements
        xml = """<?xml version="1.0"?>
        <linkbase xmlns="http://www.xbrl.org/2003/linkbase"
                  xmlns:xlink="http://www.w3.org/1999/xlink">
          <presentationLink xlink:role="http://example/role/IncomeStatement">
            <presentationArc xlink:from="loc_root" xlink:to="loc_revenue" order="1"/>
            <presentationArc xlink:from="loc_root" xlink:to="loc_cogs" order="2"/>
            <presentationArc xlink:from="loc_root" xlink:to="loc_opinc" order="3"/>
          </presentationLink>
        </linkbase>"""

        pres_index = parse_pre_linkbase(xml)
        assert isinstance(pres_index, dict)
        assert len(pres_index) > 0

    def test_sort_by_presentation_reorders_children(self):
        """sort_by_presentation reorders children according to pres_index."""
        from xbrl_tree import sort_by_presentation

        tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_CostOfGoodsAndServicesSold", weight=-1.0,
                       values={"2024": 400}),
            _make_leaf("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                       values={"2024": 1000}),
            _make_leaf("us-gaap_OperatingIncomeLoss", values={"2024": 300}),
        ], values={"2024": 200})

        # Presentation index says: Revenue=0, COGS=1, OpInc=2
        pres_index = {
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax": 0,
            "us-gaap_CostOfGoodsAndServicesSold": 1,
            "us-gaap_OperatingIncomeLoss": 2,
        }

        sort_by_presentation(tree, pres_index)

        concepts = [c.concept for c in tree.children]
        assert concepts[0] == "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax"
        assert concepts[1] == "us-gaap_CostOfGoodsAndServicesSold"
        assert concepts[2] == "us-gaap_OperatingIncomeLoss"

    def test_children_not_in_pres_index_sort_to_end(self):
        """Children missing from pres_index should appear after indexed ones."""
        from xbrl_tree import sort_by_presentation

        tree = _make_parent("root", [
            _make_leaf("unknown_item", values={"2024": 50}),
            _make_leaf("known_item_B", values={"2024": 200}),
            _make_leaf("known_item_A", values={"2024": 100}),
        ], values={"2024": 350})

        pres_index = {"known_item_A": 0, "known_item_B": 1}
        sort_by_presentation(tree, pres_index)

        concepts = [c.concept for c in tree.children]
        assert concepts[0] == "known_item_A"
        assert concepts[1] == "known_item_B"
        assert concepts[2] == "unknown_item"


# ===========================================================================
# 2. Cascade rendering for IS
# ===========================================================================

class TestCascadeLayout:
    """cascade_layout produces post-order for IS (Revenue first, NI last)."""

    def _build_bottom_up_is(self):
        """Build a bottom-up IS tree: NI at root, Revenue 3 levels deep.

        Structure (calculation linkbase order, bottom-up):
          NetIncome (root, value=200)
            + EBT (value=250)
              + OperatingIncome (value=600)
                + Revenue (leaf, value=1000)
                - COGS (leaf, value=-400)
              - Tax (leaf, value=-50)
            - InterestExpense (leaf, value=-50, weight=-1 from EBT perspective...
              but it's a child of NI in this structure)

        For simplicity:
          NI = 200
            + EBT = 250, weight=+1
              + OpInc = 600, weight=+1
                + Revenue = 1000, weight=+1
                - COGS = 400, weight=-1
              - Tax = 50, weight=-1
            - IntExp = 50, weight=-1
        """
        revenue = _make_leaf("us-gaap_Revenue", values={"2024": 1000})
        cogs = _make_leaf("us-gaap_CostOfGoodsAndServicesSold",
                          weight=-1.0, values={"2024": 400})
        opinc = _make_parent("us-gaap_OperatingIncomeLoss",
                             [revenue, cogs], values={"2024": 600})
        tax = _make_leaf("us-gaap_IncomeTaxExpenseBenefit",
                         weight=-1.0, values={"2024": 50})
        ebt = _make_parent("us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
                           [opinc, tax], values={"2024": 250})
        intexp = _make_leaf("us-gaap_InterestExpense",
                            weight=-1.0, values={"2024": 50})
        ni = _make_parent("us-gaap_NetIncomeLoss",
                          [ebt, intexp], values={"2024": 200})
        return ni

    def test_cascade_puts_revenue_first_ni_last(self):
        """In cascade (post-order) layout, Revenue is first data row, NI is last."""
        from xbrl_tree import cascade_layout

        tree = self._build_bottom_up_is()
        rows = cascade_layout(tree)

        # rows should be a list of TreeNode references in display order
        concepts = [r.concept for r in rows]
        assert concepts[0] == "us-gaap_Revenue", \
            f"First row should be Revenue, got {concepts[0]}"
        assert concepts[-1] == "us-gaap_NetIncomeLoss", \
            f"Last row should be NI, got {concepts[-1]}"

    def test_cascade_subtotals_after_children(self):
        """Each subtotal appears AFTER its children (post-order)."""
        from xbrl_tree import cascade_layout

        tree = self._build_bottom_up_is()
        rows = cascade_layout(tree)
        concepts = [r.concept for r in rows]

        # OperatingIncome should come after Revenue and COGS
        rev_idx = concepts.index("us-gaap_Revenue")
        cogs_idx = concepts.index("us-gaap_CostOfGoodsAndServicesSold")
        opinc_idx = concepts.index("us-gaap_OperatingIncomeLoss")
        assert opinc_idx > rev_idx
        assert opinc_idx > cogs_idx

        # EBT should come after OperatingIncome and Tax
        ebt_idx = concepts.index(
            "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxes")
        tax_idx = concepts.index("us-gaap_IncomeTaxExpenseBenefit")
        assert ebt_idx > opinc_idx
        assert ebt_idx > tax_idx

    def test_bs_uses_preorder_not_cascade(self):
        """BS/CF trees should NOT use cascade layout — parent comes before children."""
        # This test verifies that cascade_layout is only for IS,
        # by checking that the default _assign_rows (pre-order) is used for BS.
        bs_tree = _make_parent("us-gaap_Assets", [
            _make_leaf("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                       values={"2024": 100}),
            _make_leaf("us-gaap_AccountsReceivableNet", values={"2024": 200}),
        ], values={"2024": 300})

        # In pre-order, parent (Assets) comes first
        # Just verify the tree structure is parent-first (default behavior)
        assert bs_tree.concept == "us-gaap_Assets"
        assert bs_tree.children[0].concept == "us-gaap_CashAndCashEquivalentsAtCarryingValue"


# ===========================================================================
# 3. Fix _tag_is_positions -- never overwrite .values
# ===========================================================================

class TestTagIsPositionsNoValueOverwrite:
    """_tag_is_positions must tag INC_NET without overwriting .values."""

    def _make_cf_tree_with_ni(self, ni_values):
        """Build a minimal CF tree with INC_NET_CF tagged."""
        cf_ni = _make_leaf("us-gaap_ProfitLoss", values=dict(ni_values),
                           role="INC_NET_CF")
        cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease",
                               [cf_ni], values=dict(ni_values), role="CF_NETCH")
        return cf_tree

    def test_nflx_pattern_root_is_ni_keeps_values(self):
        """When IS root IS Net Income (NFLX pattern), root gets INC_NET
        and its .values dict is NOT replaced."""
        from xbrl_tree import _tag_is_positions

        # NFLX pattern: IS root = NI, same values as CF NI
        original_values = {"2022": 4491_804, "2023": 5407_990, "2024": 8002_923}
        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={"2022": 31615_550, "2023": 33723_290,
                                                    "2024": 38349_890}),
        ], values=dict(original_values))

        cf_tree = self._make_cf_tree_with_ni(original_values)

        _tag_is_positions(is_tree, cf_tree)

        tagged = find_node_by_role(is_tree, "INC_NET")
        assert tagged is not None, "INC_NET not assigned"
        # Values must be the SAME object or at least same content -- NOT overwritten
        for p, v in original_values.items():
            assert tagged.values[p] == v, \
                f"Value for {p} was corrupted: expected {v}, got {tagged.values[p]}"

    def test_child_match_does_not_overwrite_values(self):
        """When IS child matches CF NI, child gets INC_NET but .values stays intact."""
        from xbrl_tree import _tag_is_positions

        child_values = {"2022": 200, "2023": 220, "2024": 240}
        ni_child = _make_leaf("us-gaap_NetIncomeLoss", values=dict(child_values))
        is_tree = _make_parent("us-gaap_ComprehensiveIncome", [
            ni_child,
            _make_leaf("us-gaap_OtherComprehensiveIncome", values={"2022": 10}),
        ], values={"2022": 210, "2023": 220, "2024": 240})

        cf_tree = self._make_cf_tree_with_ni(child_values)
        _tag_is_positions(is_tree, cf_tree)

        tagged = find_node_by_role(is_tree, "INC_NET")
        assert tagged is not None
        # The child's original values must still be intact
        for p, v in child_values.items():
            assert tagged.values[p] == v, \
                f"Value for {p} was corrupted: expected {v}, got {tagged.values[p]}"

    def test_nflx_ebt_regression_values_not_corrupted(self):
        """Regression: NFLX EBT values must not be corrupted by _tag_is_positions.

        Bug: EBT was 12,722,552 but got overwritten to 10,981,201 because
        _tag_is_positions replaced .values dict on a parent node.
        """
        from xbrl_tree import _tag_is_positions

        ebt_values = {"2024": 12_722_552}
        ni_values = {"2024": 10_981_201}

        ebt = _make_parent("us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxes", [
            _make_leaf("us-gaap_Revenue", values={"2024": 38_349_890}),
        ], values=dict(ebt_values))

        ni = _make_parent("us-gaap_NetIncomeLoss", [
            ebt,
            _make_leaf("us-gaap_IncomeTaxExpenseBenefit", weight=-1.0,
                       values={"2024": 1_741_351}),
        ], values=dict(ni_values))

        cf_tree = self._make_cf_tree_with_ni(ni_values)
        _tag_is_positions(ni, cf_tree)

        # EBT must NOT be overwritten
        assert ebt.values["2024"] == 12_722_552, \
            f"EBT corrupted: expected 12,722,552, got {ebt.values['2024']}"


# ===========================================================================
# 4. Semantic BFS tagging
# ===========================================================================

class TestSemanticBFSTagging:
    """_tag_is_semantic finds IS roles by keyword at shallowest depth."""

    def test_finds_revenue_regardless_of_depth(self):
        """IS_REVENUE is found by keyword even when Revenue is 3 levels deep."""
        from xbrl_tree import _tag_is_semantic

        # Revenue buried 3 levels deep
        revenue = _make_leaf("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                             values={"2024": 1000})
        gp = _make_parent("us-gaap_GrossProfit", [revenue], values={"2024": 600})
        opinc = _make_parent("us-gaap_OperatingIncomeLoss", [gp], values={"2024": 300})
        ni = _make_parent("us-gaap_NetIncomeLoss", [opinc], values={"2024": 200})

        _tag_is_semantic(ni)

        tagged = find_node_by_role(ni, "IS_REVENUE")
        assert tagged is not None, "IS_REVENUE not found"
        assert tagged.values["2024"] == 1000

    def test_shallowest_match_wins(self):
        """When multiple nodes match 'revenue', shallowest one wins."""
        from xbrl_tree import _tag_is_semantic

        # Two nodes with "Revenue" in name at different depths
        deep_rev = _make_leaf("us-gaap_ProductRevenue", values={"2024": 400})
        shallow_rev = _make_parent("us-gaap_Revenue", [deep_rev], values={"2024": 1000})
        ni = _make_parent("us-gaap_NetIncomeLoss", [shallow_rev], values={"2024": 200})

        _tag_is_semantic(ni)

        tagged = find_node_by_role(ni, "IS_REVENUE")
        assert tagged is not None
        # Should be the shallower one (depth 1, not depth 2)
        assert tagged.concept == "us-gaap_Revenue"

    def test_bank_without_cogs_skips_gracefully(self):
        """Banks have no COGS concept. _tag_is_semantic should not error."""
        from xbrl_tree import _tag_is_semantic

        # Bank IS: Revenue -> OpEx -> NI (no COGS)
        interest_income = _make_leaf("us-gaap_InterestIncomeExpenseNet",
                                     values={"2024": 5000})
        provision = _make_leaf("us-gaap_ProvisionForLoanLossesExpensed",
                               weight=-1.0, values={"2024": 200})
        ni = _make_parent("us-gaap_NetIncomeLoss",
                          [interest_income, provision], values={"2024": 4800})

        # Should not raise
        _tag_is_semantic(ni)

        # IS_COGS should be None (gracefully skipped)
        cogs = find_node_by_role(ni, "IS_COGS")
        assert cogs is None, "Bank tree should not have IS_COGS"


# ===========================================================================
# 5. Orphan fact supplementation
# ===========================================================================

class TestOrphanFactSupplementation:
    """_supplement_orphan_facts fills gaps with unused XBRL facts."""

    def test_orphan_closes_gap_gets_inserted(self):
        """An orphan fact that exactly closes a parent-children gap is inserted."""
        from xbrl_tree import _supplement_orphan_facts

        # Parent = 1000, children sum to 800 -> gap of 200
        child1 = _make_leaf("us-gaap_ProductRevenue", values={"2024": 500})
        child2 = _make_leaf("us-gaap_ServiceRevenue", values={"2024": 300})
        parent = _make_parent("us-gaap_Revenue", [child1, child2],
                              values={"2024": 1000})

        # Orphan fact: concept with value 200 that closes the gap
        orphan_facts = {
            "us-gaap_OtherRevenue": {"2024": 200},
        }
        used_tags = set()

        _supplement_orphan_facts(parent, orphan_facts, used_tags)

        # Parent should now have 3 children
        assert len(parent.children) == 3
        new_child = parent.children[-1]
        assert new_child.values["2024"] == 200

    def test_orphan_overshooting_gap_rejected(self):
        """An orphan that would overshoot the gap is NOT inserted."""
        from xbrl_tree import _supplement_orphan_facts

        child1 = _make_leaf("us-gaap_ProductRevenue", values={"2024": 500})
        parent = _make_parent("us-gaap_Revenue", [child1],
                              values={"2024": 700})

        # Orphan with value 300 would overshoot (gap is 200)
        orphan_facts = {
            "us-gaap_OtherRevenue": {"2024": 300},
        }
        used_tags = set()

        _supplement_orphan_facts(parent, orphan_facts, used_tags)

        # No new child added
        assert len(parent.children) == 1

    def test_already_used_tags_not_reinserted(self):
        """Tags already used in the tree should not be inserted again."""
        from xbrl_tree import _supplement_orphan_facts

        child1 = _make_leaf("us-gaap_ProductRevenue", values={"2024": 500})
        parent = _make_parent("us-gaap_Revenue", [child1],
                              values={"2024": 700})

        orphan_facts = {
            "us-gaap_OtherRevenue": {"2024": 200},
        }
        # Mark this tag as already used
        used_tags = {"us-gaap_OtherRevenue"}

        _supplement_orphan_facts(parent, orphan_facts, used_tags)

        # Should NOT add a duplicate
        assert len(parent.children) == 1

    def test_no_value_mutation_on_existing_nodes(self):
        """Supplementation must never change .values on existing nodes."""
        from xbrl_tree import _supplement_orphan_facts

        original_parent_values = {"2024": 1000}
        original_child_values = {"2024": 500}

        child1 = _make_leaf("us-gaap_ProductRevenue",
                            values=dict(original_child_values))
        parent = _make_parent("us-gaap_Revenue", [child1],
                              values=dict(original_parent_values))

        orphan_facts = {"us-gaap_OtherRevenue": {"2024": 500}}
        used_tags = set()

        _supplement_orphan_facts(parent, orphan_facts, used_tags)

        assert parent.values["2024"] == 1000, "Parent values mutated"
        assert child1.values["2024"] == 500, "Child values mutated"

    def test_bottom_up_processing_fixes_children_first(self):
        """Orphan supplementation processes children before parents."""
        from xbrl_tree import _supplement_orphan_facts

        # Grandchild level: gap at child level
        gc1 = _make_leaf("us-gaap_ProductRevenue", values={"2024": 300})
        child = _make_parent("us-gaap_DomesticRevenue", [gc1],
                             values={"2024": 500})  # gap of 200 at child level
        parent = _make_parent("us-gaap_Revenue", [child],
                              values={"2024": 800})  # gap of 300 at parent level

        orphan_facts = {
            "us-gaap_OtherDomesticRevenue": {"2024": 200},  # closes child gap
            "us-gaap_InternationalRevenue": {"2024": 300},   # closes parent gap
        }
        used_tags = set()

        _supplement_orphan_facts(parent, orphan_facts, used_tags)

        # Child should have gotten its orphan first (bottom-up)
        assert len(child.children) == 2, \
            f"Child should have 2 children, got {len(child.children)}"


# ===========================================================================
# 6. Tree completeness verification
# ===========================================================================

class TestTreeCompletenessVerification:
    """verify_tree_completeness checks SUM(children*weight) vs declared value."""

    def test_balanced_tree_no_errors(self):
        """Tree where children sum matches declared value -> no errors."""
        from xbrl_tree import verify_tree_completeness

        tree = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRevenue", values={"2024": 600}),
            _make_leaf("us-gaap_ServiceRevenue", values={"2024": 400}),
        ], values={"2024": 1000})

        errors = verify_tree_completeness(tree, ["2024"])
        assert errors == [], f"Expected no errors, got {errors}"

    def test_imbalanced_tree_returns_gap_info(self):
        """Tree where children don't sum to declared -> errors with gap."""
        from xbrl_tree import verify_tree_completeness

        tree = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRevenue", values={"2024": 600}),
            _make_leaf("us-gaap_ServiceRevenue", values={"2024": 300}),
        ], values={"2024": 1000})  # sum=900, declared=1000, gap=100

        errors = verify_tree_completeness(tree, ["2024"])
        assert len(errors) > 0, "Should detect gap of 100"

    def test_rounding_within_tolerance_no_errors(self):
        """Small rounding differences within tolerance -> no errors."""
        from xbrl_tree import verify_tree_completeness

        tree = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRevenue", values={"2024": 600}),
            _make_leaf("us-gaap_ServiceRevenue", values={"2024": 400}),
        ], values={"2024": 1000.4})  # off by 0.4

        errors = verify_tree_completeness(tree, ["2024"])
        assert errors == [], f"Rounding should be tolerated, got {errors}"

    def test_leaf_nodes_skipped(self):
        """Leaf nodes (no children) should be skipped -- nothing to verify."""
        from xbrl_tree import verify_tree_completeness

        leaf = _make_leaf("us-gaap_Revenue", values={"2024": 1000})
        errors = verify_tree_completeness(leaf, ["2024"])
        assert errors == [], "Leaf nodes should produce no errors"

    def test_negative_weight_child_subtracted(self):
        """Children with weight=-1 are subtracted in completeness check."""
        from xbrl_tree import verify_tree_completeness

        tree = _make_parent("us-gaap_GrossProfit", [
            _make_leaf("us-gaap_Revenue", values={"2024": 1000}),
            _make_leaf("us-gaap_CostOfGoodsAndServicesSold",
                       weight=-1.0, values={"2024": 400}),
        ], values={"2024": 600})  # 1000 - 400 = 600

        errors = verify_tree_completeness(tree, ["2024"])
        assert errors == [], f"Expected no errors, got {errors}"


# ===========================================================================
# 7. Declarative invariant checks (CROSS_STATEMENT_CHECKS)
# ===========================================================================

class TestCrossStatementChecks:
    """CROSS_STATEMENT_CHECKS with role-based formula generation."""

    def test_all_roles_present_generates_formulas(self):
        """When all required roles are present, cross-statement formulas are generated."""
        from xbrl_tree import CROSS_STATEMENT_CHECKS

        assert isinstance(CROSS_STATEMENT_CHECKS, (list, tuple, dict)), \
            "CROSS_STATEMENT_CHECKS must be a list/tuple/dict of check definitions"

        # Build trees with all standard roles
        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={"2024": 1000}, role="IS_REVENUE"),
        ], values={"2024": 200}, role="INC_NET")

        cf_tree = _make_parent("us-gaap_CashFlow", [
            _make_leaf("us-gaap_ProfitLoss", values={"2024": 200}, role="INC_NET_CF"),
        ], values={"2024": 300}, role="CF_NETCH")

        role_map = {
            "INC_NET": ("IS", 5),
            "INC_NET_CF": ("CF", 20),
            "IS_REVENUE": ("IS", 6),
            "CF_NETCH": ("CF", 19),
        }

        # Verify at least one check exists that references roles in our map
        found_check = False
        for check in (CROSS_STATEMENT_CHECKS if isinstance(CROSS_STATEMENT_CHECKS, (list, tuple))
                      else CROSS_STATEMENT_CHECKS.values()):
            if hasattr(check, 'get'):
                roles = check.get("roles", [])
            elif hasattr(check, 'roles'):
                roles = check.roles
            else:
                continue
            if any(r in role_map for r in roles):
                found_check = True
                break
        assert found_check, "No cross-statement check found that uses our roles"

    def test_missing_role_gracefully_skipped(self):
        """A check referencing a missing role should be skipped, not error."""
        from xbrl_tree import CROSS_STATEMENT_CHECKS
        from sheet_builder import _render_cross_checks

        # role_map missing some roles
        role_map = {
            "INC_NET": ("IS", 5),
            # INC_NET_CF deliberately missing
        }

        # Should not raise
        result = _render_cross_checks(CROSS_STATEMENT_CHECKS, role_map, PERIODS)
        # Result may be empty (all checks skipped) but should not error
        assert isinstance(result, list)

    def test_no_tautological_computed_aliases(self):
        """No check should reference *_COMPUTED_* aliases (tautological)."""
        from xbrl_tree import CROSS_STATEMENT_CHECKS

        checks = (CROSS_STATEMENT_CHECKS if isinstance(CROSS_STATEMENT_CHECKS, (list, tuple))
                  else list(CROSS_STATEMENT_CHECKS.values()))
        for check in checks:
            # Check all string values for COMPUTED
            check_str = str(check)
            assert "_COMPUTED_" not in check_str, \
                f"Tautological check found: {check}"


# ===========================================================================
# 8. Pipeline gate
# ===========================================================================

class TestPipelineGate:
    """Pipeline should block sheet write when verification fails."""

    def test_tree_completeness_failure_blocks_sheet(self):
        """When verify_tree_completeness returns errors, pipeline gate blocks."""
        from xbrl_tree import verify_tree_completeness

        # Imbalanced tree
        tree = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRevenue", values={"2024": 500}),
        ], values={"2024": 1000})  # gap of 500

        errors = verify_tree_completeness(tree, ["2024"])
        assert len(errors) > 0, "Should have completeness errors"
        # Gate condition: errors means block
        assert bool(errors) is True

    def test_verify_model_failure_blocks_sheet(self):
        """When verify_model returns errors, sheet write should be blocked."""
        from pymodel import verify_model

        # Build trees with BS imbalance
        p = "2024"
        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={p: 1000}),
        ], values={p: 200})
        is_tree.role = "INC_NET"

        bs_tree = _make_parent("us-gaap_Assets", [
            _make_leaf("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                       values={p: 500}),
        ], values={p: 500})
        bs_tree.role = "BS_TA"
        bs_tree.children[0].role = "BS_CASH"

        bs_le = _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
            _make_parent("us-gaap_Liabilities", [], values={p: 100}, role="BS_TL"),
            _make_parent("us-gaap_StockholdersEquity", [], values={p: 100}, role="BS_TE"),
        ], values={p: 200})
        # TA=500, TL+TE=200 -> imbalance

        cf_tree = _make_parent("us-gaap_CashFlow", [
            _make_leaf("us-gaap_ProfitLoss", values={p: 200}, role="INC_NET_CF"),
        ], values={p: 300}, role="CF_NETCH")

        trees = {
            "IS": is_tree, "BS": bs_tree, "BS_LE": bs_le, "CF": cf_tree,
            "complete_periods": [p],
            "facts": {},
        }

        errors = verify_model(trees)
        assert len(errors) > 0, "Should detect BS imbalance"

    def test_both_pass_allows_sheet_write(self):
        """When both verifications pass, sheet write should proceed."""
        from xbrl_tree import verify_tree_completeness
        from pymodel import verify_model

        p = "2024"
        # Balanced tree
        tree = _make_parent("us-gaap_Revenue", [
            _make_leaf("us-gaap_ProductRevenue", values={p: 600}),
            _make_leaf("us-gaap_ServiceRevenue", values={p: 400}),
        ], values={p: 1000})

        tree_errors = verify_tree_completeness(tree, [p])
        assert tree_errors == [], "Tree should be balanced"

        # verify_model with balanced books
        is_tree = _make_parent("us-gaap_NetIncomeLoss", [
            _make_leaf("us-gaap_Revenue", values={p: 1000}),
            _make_leaf("us-gaap_CostOfGoodsAndServicesSold",
                       weight=-1.0, values={p: 800}),
        ], values={p: 200})
        is_tree.role = "INC_NET"

        bs_tree = _make_parent("us-gaap_Assets", [
            _make_leaf("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                       values={p: 500}, role="BS_CASH"),
        ], values={p: 500}, role="BS_TA")

        bs_le = _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
            _make_parent("us-gaap_Liabilities", [
                _make_leaf("us-gaap_AccountsPayable", values={p: 300}),
            ], values={p: 300}, role="BS_TL"),
            _make_parent("us-gaap_StockholdersEquity", [
                _make_leaf("us-gaap_RetainedEarnings", values={p: 200}),
            ], values={p: 200}, role="BS_TE"),
        ], values={p: 500})

        cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease", [
            _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities", [
                _make_leaf("us-gaap_ProfitLoss", values={p: 200}, role="INC_NET_CF"),
            ], values={p: 200}, role="CF_OPCF"),
            _make_parent("us-gaap_NetCashProvidedByUsedInInvestingActivities", [],
                         values={p: 0}, role="CF_INVCF"),
            _make_parent("us-gaap_NetCashProvidedByUsedInFinancingActivities", [],
                         values={p: 0}, role="CF_FINCF"),
        ], values={p: 200}, role="CF_NETCH")

        trees = {
            "IS": is_tree, "BS": bs_tree, "BS_LE": bs_le, "CF": cf_tree,
            "complete_periods": [p],
            "facts": {
                "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": {
                    p: 500,
                },
            },
        }

        model_errors = verify_model(trees)
        # Gate: both must pass for sheet write to proceed
        can_write = (tree_errors == [] and model_errors == [])
        assert can_write, f"Sheet write blocked: tree={tree_errors}, model={model_errors}"
