"""
Phase 2 Tests: Tree-First Architecture
=======================================
Tests reconcile_trees(), verify_model(), and sheet_builder rendering
using synthetic tree fixtures. No network access required.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbrl_tree import TreeNode, reconcile_trees, find_node_by_role
from pymodel import verify_model


# ---------------------------------------------------------------------------
# Fixture helpers
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


def _build_synthetic_trees():
    """Build a minimal but realistic 3-statement tree set.

    IS: NetIncome (root, -1 weight children)
        + Revenue (leaf, 1000/1100)
        - COGS (leaf, -400/-450)
        + GrossProfit (leaf, 600/650)  -- NI node (matches CF's NI)
        ... simplified: NI = root's first positive child = 200/220

    Actually, let's build it closer to real XBRL structure:
    IS root = NetIncomeLoss (value = 200/220)
      + Revenue (weight=+1, value=1000/1100)
      - CostOfRevenue (weight=-1, value=400/450)
      + GrossProfit (weight=+1, value=600/650)  <-- not NI
      - OperatingExpenses (weight=-1, value=350/380)
      + OperatingIncome (weight=+1, value=250/270)
      - IncomeTaxExpense (weight=-1, value=50/50)

    The IS root itself has value=200/220 which equals NI.
    But structurally, we need a depth-1 child tagged as INC_NET.
    Real XBRL: root = NI, children contribute to it.

    Let's simplify: IS root = NI with value matching CF's NI.
    """
    periods = {"2023-12-31": True, "2022-12-31": True}
    p1, p2 = "2023-12-31", "2022-12-31"

    # --- Income Statement ---
    rev = _make_leaf("us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                     values={p1: 1000, p2: 900})
    cogs = _make_leaf("us-gaap_CostOfGoodsAndServicesSold", weight=-1.0,
                      values={p1: 400, p2: 360})
    opex = _make_leaf("us-gaap_OperatingExpenses", weight=-1.0,
                      values={p1: 350, p2: 310})
    tax = _make_leaf("us-gaap_IncomeTaxExpenseBenefit", weight=-1.0,
                     values={p1: 50, p2: 46})
    # NI = 1000 - 400 - 350 - 50 = 200
    is_root = _make_parent("us-gaap_NetIncomeLoss",
                           [rev, cogs, opex, tax],
                           values={p1: 200, p2: 184})

    # --- Balance Sheet (Assets) ---
    cash = _make_leaf("us-gaap_CashAndCashEquivalentsAtCarryingValue",
                      values={p1: 100, p2: 90})  # Will be overridden by CF_ENDC
    ar = _make_leaf("us-gaap_AccountsReceivableNet",
                    values={p1: 150, p2: 130})
    tca = _make_parent("us-gaap_AssetsCurrent", [cash, ar],
                       values={p1: 250, p2: 220})
    ppe = _make_leaf("us-gaap_PropertyPlantAndEquipmentNet",
                     values={p1: 500, p2: 480})
    tnca = _make_parent("us-gaap_AssetsNoncurrent", [ppe],
                        values={p1: 500, p2: 480})
    bs_assets = _make_parent("us-gaap_Assets", [tca, tnca],
                             values={p1: 750, p2: 700})

    # --- Balance Sheet (L&E) ---
    ap = _make_leaf("us-gaap_AccountsPayable",
                    values={p1: 80, p2: 70})
    tcl = _make_parent("us-gaap_LiabilitiesCurrent", [ap],
                       values={p1: 80, p2: 70})
    ltd = _make_leaf("us-gaap_LongTermDebt",
                     values={p1: 200, p2: 210})
    tncl = _make_parent("us-gaap_LiabilitiesNoncurrent", [ltd],
                        values={p1: 200, p2: 210})
    tl = _make_parent("us-gaap_Liabilities", [tcl, tncl],
                      values={p1: 280, p2: 280})
    re = _make_leaf("us-gaap_RetainedEarningsAccumulatedDeficit",
                    values={p1: 470, p2: 420})
    te = _make_parent("us-gaap_StockholdersEquity", [re],
                      values={p1: 470, p2: 420})
    bs_le = _make_parent("us-gaap_LiabilitiesAndStockholdersEquity", [tl, te],
                         values={p1: 750, p2: 700})

    # --- Cash Flow ---
    cf_ni = _make_leaf("us-gaap_ProfitLoss",
                       values={p1: 200, p2: 184})
    da = _make_leaf("us-gaap_DepreciationDepletionAndAmortization",
                    values={p1: 30, p2: 28})
    opcf = _make_parent("us-gaap_NetCashProvidedByUsedInOperatingActivities",
                        [cf_ni, da],
                        values={p1: 230, p2: 212})
    capex = _make_leaf("us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                       weight=-1.0, values={p1: -50, p2: -40})
    invcf = _make_parent("us-gaap_NetCashProvidedByUsedInInvestingActivities",
                         [capex], values={p1: -50, p2: -40})
    divs = _make_leaf("us-gaap_PaymentsOfDividends", weight=-1.0,
                      values={p1: -60, p2: -55})
    fincf = _make_parent("us-gaap_NetCashProvidedByUsedInFinancingActivities",
                         [divs], values={p1: -60, p2: -55})
    # NETCH = 230 - 50 - 60 = 120
    cf_root = _make_parent("us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
                           [opcf, invcf, fincf],
                           values={p1: 120, p2: 117})

    # Facts dict (includes CF_ENDC as instant context)
    facts = {
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": {
            p1: 120, p2: 90,  # Different from BS cash to test override
        },
    }

    return {
        "IS": is_root,
        "BS": bs_assets,
        "BS_LE": bs_le,
        "CF": cf_root,
        "facts": facts,
        "periods": [p2, p1],
    }


# ---------------------------------------------------------------------------
# Test 1: reconcile_trees identifies positional roles
# ---------------------------------------------------------------------------

def test_reconcile_identifies_bs_positions():
    """reconcile_trees tags BS_TA, BS_TL, BS_TE by position."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    assert find_node_by_role(trees["BS"], "BS_TA") is not None
    assert find_node_by_role(trees["BS"], "BS_TCA") is not None
    assert find_node_by_role(trees["BS"], "BS_CASH") is not None
    assert find_node_by_role(trees["BS_LE"], "BS_TL") is not None
    assert find_node_by_role(trees["BS_LE"], "BS_TE") is not None

    # BS_TA should be the root
    assert trees["BS"].role == "BS_TA"


def test_reconcile_identifies_cf_positions():
    """reconcile_trees tags CF_NETCH, CF_OPCF, CF_INVCF, CF_FINCF, INC_NET_CF."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    assert trees["CF"].role == "CF_NETCH"
    assert find_node_by_role(trees["CF"], "CF_OPCF") is not None
    assert find_node_by_role(trees["CF"], "CF_INVCF") is not None
    assert find_node_by_role(trees["CF"], "CF_FINCF") is not None
    assert find_node_by_role(trees["CF"], "INC_NET_CF") is not None


def test_reconcile_identifies_is_net_income():
    """reconcile_trees tags INC_NET on the IS tree."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    inc_net = find_node_by_role(trees["IS"], "INC_NET")
    assert inc_net is not None


# ---------------------------------------------------------------------------
# Test 2: reconcile_trees overrides BS cash with CF_ENDC
# ---------------------------------------------------------------------------

def test_reconcile_overrides_cash():
    """BS cash values should be overridden with CF_ENDC values after reconciliation."""
    trees = _build_synthetic_trees()
    cf_endc = trees["facts"]["us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]

    reconcile_trees(trees)

    bs_cash = find_node_by_role(trees["BS"], "BS_CASH")
    assert bs_cash is not None
    for period, expected_val in cf_endc.items():
        assert bs_cash.values[period] == expected_val, \
            f"BS_CASH not overridden for {period}: got {bs_cash.values[period]}, expected {expected_val}"


def test_reconcile_adjusts_tca_after_cash_override():
    """TCA subtotal should be adjusted when BS cash is overridden."""
    trees = _build_synthetic_trees()
    p1 = "2023-12-31"

    # Before reconciliation: cash=100, AR=150, TCA=250
    # CF_ENDC for p1 = 120 (delta = +20)
    # After: TCA should be 250 + 20 = 270
    reconcile_trees(trees)

    tca = find_node_by_role(trees["BS"], "BS_TCA")
    assert tca.values[p1] == 270, f"TCA not adjusted: got {tca.values[p1]}, expected 270"


# ---------------------------------------------------------------------------
# Test 3: IS NI value-matching against CF's NI
# ---------------------------------------------------------------------------

def test_is_ni_value_matches_cf_ni():
    """INC_NET on IS should have the same values as INC_NET_CF on CF."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    inc_net_is = find_node_by_role(trees["IS"], "INC_NET")
    inc_net_cf = find_node_by_role(trees["CF"], "INC_NET_CF")

    assert inc_net_is is not None
    assert inc_net_cf is not None
    for p in trees.get("complete_periods", []):
        assert inc_net_is.values.get(p) == inc_net_cf.values.get(p), \
            f"NI mismatch in {p}: IS={inc_net_is.values.get(p)}, CF={inc_net_cf.values.get(p)}"


def test_is_ni_tags_correct_node_when_multiple_positive_children():
    """When IS has multiple positive-weight children, tag the one whose values match CF NI."""
    trees = _build_synthetic_trees()
    p1, p2 = "2023-12-31", "2022-12-31"

    # Restructure IS: root has two positive-weight children —
    # ComprehensiveIncome (different values) and NetIncome (matching CF NI).
    # Value-matching should pick NetIncome, not ComprehensiveIncome.
    ni_child = _make_leaf("us-gaap_NetIncomeLoss",
                          values={p1: 200, p2: 184})  # Matches CF NI
    comp_income = _make_leaf("us-gaap_ComprehensiveIncomeNetOfTax",
                             values={p1: 210, p2: 190})  # Different

    # Build a new IS root with both children (comp_income first)
    is_root = _make_parent("us-gaap_ComprehensiveIncome",
                           [comp_income, ni_child],
                           values={p1: 210, p2: 190})
    trees["IS"] = is_root

    reconcile_trees(trees)

    inc_net = find_node_by_role(trees["IS"], "INC_NET")
    assert inc_net is not None
    # Should be NetIncomeLoss (200/184 matches CF NI), not ComprehensiveIncome (210/190)
    assert "NetIncomeLoss" in inc_net.concept, \
        f"Tagged wrong node: {inc_net.concept} — should be NetIncomeLoss"


# ---------------------------------------------------------------------------
# Test 4: verify_model passes on reconciled trees
# ---------------------------------------------------------------------------

def test_invariants_pass_on_reconciled_trees():
    """5 cross-statement checks should pass on properly reconciled trees."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)
    errors = verify_model(trees)
    assert errors == [], f"Invariant failures: {errors}"


def test_verify_model_catches_bs_imbalance():
    """verify_model should catch BS imbalance when TA != TL + TE."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    # Break BS balance by modifying TA
    bs_ta = find_node_by_role(trees["BS"], "BS_TA")
    bs_ta.children = []
    for p in list(bs_ta.values.keys()):
        bs_ta.values[p] += 999

    errors = verify_model(trees)
    bs_errors = [e for e in errors if "BS Balance" in e[0]]
    assert len(bs_errors) > 0, "Should detect BS imbalance"


def test_verify_model_catches_ni_mismatch():
    """verify_model should catch NI mismatch when IS NI != CF NI."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    # Break NI link by modifying IS's NI
    inc_net_is = find_node_by_role(trees["IS"], "INC_NET")
    inc_net_is.children = []  # Force fv() to use declared values
    for p in list(inc_net_is.values.keys()):
        inc_net_is.values[p] += 500

    errors = verify_model(trees)
    ni_errors = [e for e in errors if "NI Link" in e[0]]
    assert len(ni_errors) > 0, "Should detect NI mismatch"


# ---------------------------------------------------------------------------
# Test 5: complete_periods filtering
# ---------------------------------------------------------------------------

def test_complete_periods_filters_partial():
    """Only periods present in ALL statements should be in complete_periods."""
    trees = _build_synthetic_trees()
    p_extra = "2021-12-31"

    # Add an extra period to IS only
    trees["IS"].values[p_extra] = 180

    reconcile_trees(trees)

    complete = trees.get("complete_periods", [])
    assert p_extra not in complete, \
        f"Partial period {p_extra} should not be in complete_periods"
    assert "2023-12-31" in complete
    assert "2022-12-31" in complete


# ---------------------------------------------------------------------------
# Test 6: TreeNode serialization round-trip
# ---------------------------------------------------------------------------

def test_tree_roundtrip_preserves_roles():
    """TreeNode.to_dict() -> from_dict() should preserve roles."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees[stmt]
        d = tree.to_dict()
        restored = TreeNode.from_dict(d)

        # Check that roles survived the round-trip
        def _collect_roles(node):
            roles = set()
            if node.role:
                roles.add(node.role)
            for child in node.children:
                roles |= _collect_roles(child)
            return roles

        original_roles = _collect_roles(tree)
        restored_roles = _collect_roles(restored)
        assert original_roles == restored_roles, \
            f"{stmt} roles lost in round-trip: {original_roles - restored_roles}"


def test_verify_model_works_with_dict_input():
    """verify_model should handle dict input (from JSON deserialization)."""
    trees = _build_synthetic_trees()
    reconcile_trees(trees)

    # Serialize to dict (simulating JSON load)
    dict_trees = {
        "complete_periods": trees["complete_periods"],
        "facts": trees["facts"],
    }
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        dict_trees[stmt] = trees[stmt].to_dict()

    errors = verify_model(dict_trees)
    assert errors == [], f"Invariant failures with dict input: {errors}"
