"""
Phase 3 Tests: D&A, SBC, and FX node tagging
=============================================
Tests _tag_da_sbc_nodes() and FX tagging in _tag_cf_positions().
Uses synthetic tree fixtures (no network access).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, reconcile_trees, find_node_by_role


# ---------------------------------------------------------------------------
# Fixture helpers (same pattern as test_model_historical.py)
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


PERIODS = ["2020", "2021", "2022", "2023", "2024"]
DA_VALUES = {"2020": 100, "2021": 110, "2022": 120, "2023": 130, "2024": 150}
SBC_VALUES = {"2020": 50, "2021": 55, "2022": 60, "2023": 65, "2024": 70}


def _build_trees_with_da_sbc():
    """Build a minimal 3-statement tree set with D&A and SBC nodes.
    
    IS tree:
      NetIncomeLoss (root)
        + Revenue (leaf)
        - CostOfRevenue (leaf)
        + DepreciationAndAmortization (leaf)    ← should become IS_DA
        + StockBasedCompensation (leaf)          ← should become IS_SBC
    
    CF tree:
      CashAndCashEquivalentsPeriodIncreaseDecrease (root = CF_NETCH)
        + NetCashProvidedByUsedInOperatingActivities (= CF_OPCF)
            + ProfitLoss (leaf = INC_NET_CF)
            + DepreciationDepletionAndAmortization (leaf)  ← should become CF_DA
            + ShareBasedCompensation (leaf)                 ← should become CF_SBC
            + ChangesInInventory (leaf, DIFFERENT values)   ← should NOT be tagged
        + NetCashProvidedByUsedInInvestingActivities (= CF_INVCF)
            + CapitalExpenditures (leaf)
        + NetCashProvidedByUsedInFinancingActivities (= CF_FINCF)
            + DividendsPaid (leaf)
        + EffectOfExchangeRateOnCash (leaf)     ← should become CF_FX
    
    BS tree: minimal, just enough for reconcile_trees() to not error.
    """
    ni_values = {"2020": 200, "2021": 220, "2022": 240, "2023": 260, "2024": 280}
    
    # IS tree
    is_tree = _make_parent("us-gaap_NetIncomeLoss", [
        _make_leaf("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                   weight=1.0, values={"2020": 1000, "2021": 1100, "2022": 1200,
                                        "2023": 1300, "2024": 1400}),
        _make_leaf("us-gaap_CostOfGoodsAndServicesSold",
                   weight=-1.0, values={"2020": 400, "2021": 450, "2022": 500,
                                         "2023": 550, "2024": 600}),
        _make_leaf("us-gaap_DepreciationAndAmortization",
                   weight=1.0, values=DA_VALUES),
        _make_leaf("us-gaap_AllocatedShareBasedCompensationExpense",
                   weight=1.0, values=SBC_VALUES),
    ], values=ni_values)
    
    # CF tree
    # Inventory values: match D&A for 2024 only (collision test)
    inventory_values = {"2020": 30, "2021": 40, "2022": 50, "2023": 60, "2024": 150}
    
    opcf = _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities", [
        _make_leaf("us-gaap_ProfitLoss", weight=1.0, values=ni_values),
        _make_leaf("us-gaap_DepreciationDepletionAndAmortization",
                   weight=1.0, values=DA_VALUES),
        _make_leaf("us-gaap_ShareBasedCompensation",
                   weight=1.0, values=SBC_VALUES),
        _make_leaf("us-gaap_IncreaseDecreaseInInventories",
                   weight=1.0, values=inventory_values),
    ], values={"2020": 380, "2021": 425, "2022": 470, "2023": 515, "2024": 650})
    
    invcf = _make_parent("us-gaap_NetCashProvidedByUsedInInvestingActivities", [
        _make_leaf("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                   weight=-1.0, values={"2020": -80, "2021": -90, "2022": -100,
                                         "2023": -110, "2024": -120}),
    ], values={"2020": -80, "2021": -90, "2022": -100, "2023": -110, "2024": -120})
    
    fincf = _make_parent("us-gaap_NetCashProvidedByUsedInFinancingActivities", [
        _make_leaf("us-gaap_PaymentsOfDividends",
                   weight=-1.0, values={"2020": -50, "2021": -55, "2022": -60,
                                         "2023": -65, "2024": -70}),
    ], values={"2020": -50, "2021": -55, "2022": -60, "2023": -65, "2024": -70})
    
    fx_node = _make_leaf("us-gaap_EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                          weight=1.0, values={"2020": -5, "2021": -3, "2022": -8,
                                               "2023": -2, "2024": -6})
    
    cf_tree = _make_parent("us-gaap_CashCashEquivalentsPeriodIncreaseDecrease",
                            [opcf, invcf, fincf, fx_node],
                            values={"2020": 245, "2021": 277, "2022": 302,
                                    "2023": 338, "2024": 454})
    
    # Minimal BS trees
    bs_tree = _make_parent("us-gaap_Assets", [
        _make_leaf("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                   values={"2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900}),
    ], values={"2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900})
    
    bs_le_tree = _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [
        _make_parent("us-gaap_Liabilities", [
            _make_leaf("us-gaap_AccountsPayable",
                       values={"2020": 200, "2021": 250, "2022": 300, "2023": 350, "2024": 400}),
        ], values={"2020": 200, "2021": 250, "2022": 300, "2023": 350, "2024": 400}),
        _make_parent("us-gaap_StockholdersEquity", [
            _make_leaf("us-gaap_CommonStockValue",
                       values={"2020": 300, "2021": 350, "2022": 400, "2023": 450, "2024": 500}),
        ], values={"2020": 300, "2021": 350, "2022": 400, "2023": 450, "2024": 500}),
    ], values={"2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900})
    
    trees = {
        "IS": is_tree,
        "CF": cf_tree,
        "BS": bs_tree,
        "BS_LE": bs_le_tree,
        "facts": {
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": {
                "2020": 500, "2021": 600, "2022": 700, "2023": 800, "2024": 900
            }
        },
    }
    return reconcile_trees(trees)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDATagging:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.trees = _build_trees_with_da_sbc()
    
    def test_is_da_tagged(self):
        """IS tree should have a leaf with role IS_DA containing 'depreciation'."""
        node = find_node_by_role(self.trees["IS"], "IS_DA")
        assert node is not None, "IS_DA role not found"
        assert node.is_leaf, "IS_DA should be a leaf node"
        assert "depreciation" in node.name.lower()
    
    def test_cf_da_tagged(self):
        """CF tree should have a leaf with role CF_DA."""
        node = find_node_by_role(self.trees["CF"], "CF_DA")
        assert node is not None, "CF_DA role not found"
        assert node.is_leaf, "CF_DA should be a leaf node"
    
    def test_da_values_match_across_statements(self):
        """IS_DA and CF_DA values must match across all complete periods."""
        is_da = find_node_by_role(self.trees["IS"], "IS_DA")
        cf_da = find_node_by_role(self.trees["CF"], "CF_DA")
        assert is_da and cf_da
        periods = self.trees.get("complete_periods", [])
        for p in periods:
            assert abs(is_da.values.get(p, 0) - cf_da.values.get(p, 0)) < 0.5, \
                f"D&A mismatch in {p}: IS={is_da.values.get(p)} vs CF={cf_da.values.get(p)}"


class TestSBCTagging:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.trees = _build_trees_with_da_sbc()
    
    def test_is_sbc_tagged(self):
        """IS tree should have a leaf with role IS_SBC."""
        node = find_node_by_role(self.trees["IS"], "IS_SBC")
        assert node is not None, "IS_SBC role not found"
        assert node.is_leaf
        name = node.name.lower()
        assert "stock" in name or "share" in name or "compensation" in name
    
    def test_cf_sbc_tagged(self):
        """CF tree should have a leaf with role CF_SBC."""
        node = find_node_by_role(self.trees["CF"], "CF_SBC")
        assert node is not None, "CF_SBC role not found"
        assert node.is_leaf
    
    def test_sbc_values_match_across_statements(self):
        """IS_SBC and CF_SBC values must match across all complete periods."""
        is_sbc = find_node_by_role(self.trees["IS"], "IS_SBC")
        cf_sbc = find_node_by_role(self.trees["CF"], "CF_SBC")
        assert is_sbc and cf_sbc
        periods = self.trees.get("complete_periods", [])
        for p in periods:
            assert abs(is_sbc.values.get(p, 0) - cf_sbc.values.get(p, 0)) < 0.5, \
                f"SBC mismatch in {p}"


class TestCollisionSafety:
    def test_timeseries_match_rejects_single_period_collision(self):
        """A CF node matching IS D&A for ONE period but not others must NOT be tagged.
        
        In our fixture, ChangesInInventory has value 150 in 2024 (same as D&A),
        but different values in all other years. It should NOT get the CF_DA role.
        """
        trees = _build_trees_with_da_sbc()
        cf_opcf = find_node_by_role(trees["CF"], "CF_OPCF")
        
        # Find the inventory node
        inventory_node = None
        for child in cf_opcf.children:
            if "inventor" in child.name.lower():
                inventory_node = child
                break
        
        assert inventory_node is not None, "Test fixture missing inventory node"
        assert inventory_node.role != "CF_DA", \
            "Inventory node was incorrectly tagged as CF_DA (single-period collision!)"


class TestFXTagging:
    def test_cf_fx_tagged_when_present(self):
        """CF_FX role assigned when EffectOfExchangeRate concept exists."""
        trees = _build_trees_with_da_sbc()
        node = find_node_by_role(trees["CF"], "CF_FX")
        assert node is not None, "CF_FX role not found"
        assert node.values  # should have values


class TestExistingRolesPreserved:
    def test_tagging_does_not_break_existing_roles(self):
        """Adding D&A/SBC/FX tagging must not break existing role assignments."""
        trees = _build_trees_with_da_sbc()
        
        # These roles should still exist from Phase 2 reconciliation
        expected = ["BS_TA", "BS_TL", "BS_TE", "CF_OPCF", "CF_INVCF",
                    "CF_FINCF", "CF_NETCH", "INC_NET_CF"]
        for role in expected:
            # Search in the appropriate tree
            found = False
            for tree_key in ["IS", "BS", "BS_LE", "CF"]:
                tree = trees.get(tree_key)
                if tree and find_node_by_role(tree, role):
                    found = True
                    break
            assert found, f"Role {role} missing after D&A/SBC tagging"
