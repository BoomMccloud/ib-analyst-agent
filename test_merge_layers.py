"""
Test: Three-layer merge (calc + presentation + "Other")
========================================================
Proves that merging calc tree structure with presentation ordering
produces formulas where SUM(children) == declared parent value,
by construction, using an "Other" row for any gap.

Layers:
  1. CALC tree  — mathematical truth: parent→children with weights
  2. PRES order — display ordering from presentation linkbase
  3. "Other"    — residual = declared_parent - SUM(known children)

The algorithm:
  For each parent node in the calc tree:
    a) Partition children into:
       - "presented": concept exists in pres_index (display order known)
       - "unpresented": concept NOT in pres_index
    b) Sort presented children by pres_index order
    c) Place unpresented children after presented ones (keep calc order)
    d) Compute residual = parent.declared - SUM(all children * weight)
    e) If |residual| > 1.0, insert an "Other" node with value = residual
    f) Formula for parent = SUM(all children incl Other) → equals declared value

This means every parent's formula produces the exact XBRL-declared number.
All cross-statement invariants hold because they compare declared values.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from xbrl_tree import TreeNode, verify_tree_completeness


# ---------------------------------------------------------------------------
# The merge algorithm
# ---------------------------------------------------------------------------

def merge_calc_pres(tree: TreeNode, pres_index: dict[str, float],
                    periods: list[str]) -> TreeNode:
    """Merge calc tree with presentation ordering. Adds 'Other' rows for gaps.

    Recurses bottom-up so children are fixed before we compute the parent's gap.
    Returns the same tree, mutated in place.
    """
    # Recurse into children first (bottom-up)
    for child in list(tree.children):
        merge_calc_pres(child, pres_index, periods)

    if not tree.children:
        return tree  # Leaf — nothing to do

    # --- Partition children ---
    presented = []
    unpresented = []
    for child in tree.children:
        if child.concept in pres_index:
            presented.append(child)
        else:
            unpresented.append(child)

    # Sort presented children by presentation order
    presented.sort(key=lambda c: pres_index.get(c.concept, 999))

    # Reorder: presented first, then unpresented (original calc order preserved)
    tree.children = presented + unpresented

    # --- Compute residual per period ---
    residual_values = {}
    has_nonzero_residual = False
    for period in periods:
        declared = tree.values.get(period, 0)
        if declared == 0:
            continue
        computed = sum(c.values.get(period, 0) * c.weight for c in tree.children)
        gap = declared - computed
        residual_values[period] = gap
        if abs(gap) > 1.0:
            has_nonzero_residual = True

    # --- Insert "Other" node if there's a gap ---
    if has_nonzero_residual:
        other = TreeNode("__OTHER__", weight=1.0)
        other.name = "Other"
        other.values = residual_values
        other.is_leaf = True
        # Place Other at the end
        tree.add_child(other)

    return tree


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def build_test_tree(concept, weight, values, children=None):
    """Quick tree builder for tests."""
    node = TreeNode(concept, weight)
    node.values = dict(values)
    if children:
        for child in children:
            node.add_child(child)
    return node


def fmt(v):
    return f"{v:>12,.0f}" if v is not None else f"{'—':>12s}"


def print_merged_tree(node, indent=0, periods=None):
    """Print tree showing formula vs declared."""
    prefix = "  " * indent
    sign = {1.0: "+", -1.0: "-"}.get(node.weight, "?")
    if indent > 0:
        prefix += f"{sign} "

    vals = ""
    if periods:
        vals = "  ".join(fmt(node.values.get(p)) for p in periods)

    label = f"{'[OTHER]' if node.concept == '__OTHER__' else node.name}"
    print(f"{prefix}{label:40s}  {vals}")

    for child in node.children:
        print_merged_tree(child, indent + 1, periods)

    # After printing children, show formula check for branch nodes
    if node.children and periods:
        for p in periods:
            declared = node.values.get(p, 0)
            computed = sum(c.values.get(p, 0) * c.weight for c in node.children)
            if declared != 0:
                gap = declared - computed
                if abs(gap) > 0.5:
                    print(f"{prefix}  *** GAP: declared={declared:,.0f} computed={computed:,.0f} gap={gap:,.0f}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_1_simple_gap():
    """Parent has 3 children in calc but declared is higher → Other fills gap."""
    print("\n" + "=" * 70)
    print("TEST 1: Simple gap → Other fills it")
    print("=" * 70)

    periods = ["2024", "2023"]

    child_a = build_test_tree("us-gaap_CashAndEquivalents", 1.0, {"2024": 100, "2023": 90})
    child_b = build_test_tree("us-gaap_ShortTermInvestments", 1.0, {"2024": 200, "2023": 180})
    child_c = build_test_tree("us-gaap_AccountsReceivable", 1.0, {"2024": 150, "2023": 140})
    # Children sum: 450 / 410 but parent declared: 500 / 450 → gap of 50 / 40
    parent = build_test_tree("us-gaap_CurrentAssets", 1.0,
                             {"2024": 500, "2023": 450},
                             [child_a, child_b, child_c])

    pres_index = {
        "us-gaap_CashAndEquivalents": 1.0,
        "us-gaap_AccountsReceivable": 2.0,
        "us-gaap_ShortTermInvestments": 3.0,
    }

    merge_calc_pres(parent, pres_index, periods)
    print_merged_tree(parent, periods=periods)

    # Verify: formula matches declared for ALL periods
    errors = verify_tree_completeness(parent, periods)
    assert not errors, f"FAIL: {errors}"
    # Verify: presentation order respected
    assert parent.children[0].concept == "us-gaap_CashAndEquivalents"
    assert parent.children[1].concept == "us-gaap_AccountsReceivable"
    assert parent.children[2].concept == "us-gaap_ShortTermInvestments"
    assert parent.children[3].concept == "__OTHER__"
    assert parent.children[3].values["2024"] == 50
    assert parent.children[3].values["2023"] == 40
    print("PASS ✓")


def test_2_no_gap():
    """Children sum exactly to parent → no Other row needed."""
    print("\n" + "=" * 70)
    print("TEST 2: No gap → no Other row")
    print("=" * 70)

    periods = ["2024"]

    child_a = build_test_tree("A", 1.0, {"2024": 300})
    child_b = build_test_tree("B", 1.0, {"2024": 200})
    parent = build_test_tree("Total", 1.0, {"2024": 500}, [child_a, child_b])

    merge_calc_pres(parent, {"A": 1.0, "B": 2.0}, periods)
    print_merged_tree(parent, periods=periods)

    assert len(parent.children) == 2, f"Expected 2 children, got {len(parent.children)}"
    errors = verify_tree_completeness(parent, periods)
    assert not errors, f"FAIL: {errors}"
    print("PASS ✓")


def test_3_subtraction_weight():
    """Child with weight=-1 (e.g., COGS). Other must account for signed math."""
    print("\n" + "=" * 70)
    print("TEST 3: Subtraction weight (-1)")
    print("=" * 70)

    periods = ["2024"]

    # GrossProfit = Revenue(+1) + COGS(-1)  → 1000 + (-700)*(-1) = 1000+700? No.
    # Actually in XBRL: parent = Revenue * 1 + COGS * (-1) = 1000 - 700 = 300
    revenue = build_test_tree("Revenue", 1.0, {"2024": 1000})
    cogs = build_test_tree("COGS", -1.0, {"2024": 700})
    # But declared GP is 250 (there's a 50 gap — maybe some deduction not in calc)
    gp = build_test_tree("GrossProfit", 1.0, {"2024": 250}, [revenue, cogs])

    merge_calc_pres(gp, {"Revenue": 1.0, "COGS": 2.0}, periods)
    print_merged_tree(gp, periods=periods)

    errors = verify_tree_completeness(gp, periods)
    assert not errors, f"FAIL: {errors}"
    # Other = 250 - (1000 * 1 + 700 * -1) = 250 - 300 = -50
    other = [c for c in gp.children if c.concept == "__OTHER__"][0]
    assert other.values["2024"] == -50, f"Expected -50, got {other.values['2024']}"
    print("PASS ✓")


def test_4_unpresented_children():
    """Some calc children not in pres_index → placed after presented ones."""
    print("\n" + "=" * 70)
    print("TEST 4: Unpresented children placed after presented")
    print("=" * 70)

    periods = ["2024"]

    a = build_test_tree("A_Presented", 1.0, {"2024": 100})
    b = build_test_tree("B_NotInPres", 1.0, {"2024": 50})
    c = build_test_tree("C_Presented", 1.0, {"2024": 80})
    d = build_test_tree("D_NotInPres", 1.0, {"2024": 70})
    parent = build_test_tree("Total", 1.0, {"2024": 300},
                             [a, b, c, d])

    # Only A and C are in presentation
    pres_index = {"A_Presented": 2.0, "C_Presented": 1.0}

    merge_calc_pres(parent, pres_index, periods)
    print_merged_tree(parent, periods=periods)

    # Order: C(pres 1.0), A(pres 2.0), B(unpres), D(unpres)
    assert parent.children[0].concept == "C_Presented"
    assert parent.children[1].concept == "A_Presented"
    assert parent.children[2].concept == "B_NotInPres"
    assert parent.children[3].concept == "D_NotInPres"
    # No gap: 100+50+80+70 = 300
    assert len(parent.children) == 4  # No Other
    errors = verify_tree_completeness(parent, periods)
    assert not errors
    print("PASS ✓")


def test_5_nested_gaps():
    """Gaps at multiple levels of nesting. Bottom-up ensures parent sees fixed children."""
    print("\n" + "=" * 70)
    print("TEST 5: Nested gaps (bottom-up)")
    print("=" * 70)

    periods = ["2024"]

    # Level 2: leaf children
    leaf1 = build_test_tree("Leaf1", 1.0, {"2024": 60})
    leaf2 = build_test_tree("Leaf2", 1.0, {"2024": 30})
    # Subtotal declared 100, children sum 90 → gap 10
    subtotal = build_test_tree("Subtotal", 1.0, {"2024": 100}, [leaf1, leaf2])

    # Level 1: subtotal + another leaf
    other_leaf = build_test_tree("OtherLeaf", 1.0, {"2024": 150})
    # Total declared 280, children sum 100+150=250 → gap 30
    total = build_test_tree("Total", 1.0, {"2024": 280}, [subtotal, other_leaf])

    pres_index = {"Leaf1": 1.0, "Leaf2": 2.0, "Subtotal": 1.0, "OtherLeaf": 2.0}

    merge_calc_pres(total, pres_index, periods)
    print_merged_tree(total, periods=periods)

    # Both levels should have Other rows
    errors = verify_tree_completeness(total, periods)
    assert not errors, f"FAIL: {errors}"

    # Subtotal should have an Other child (gap=10)
    subtotal_other = [c for c in subtotal.children if c.concept == "__OTHER__"]
    assert len(subtotal_other) == 1
    assert subtotal_other[0].values["2024"] == 10

    # Total should have an Other child (gap=30)
    total_other = [c for c in total.children if c.concept == "__OTHER__"]
    assert len(total_other) == 1
    assert total_other[0].values["2024"] == 30

    print("PASS ✓")


def test_6_cross_statement_invariants():
    """Simulate BS balance check: TA = TL + TE after merge. Must be zero."""
    print("\n" + "=" * 70)
    print("TEST 6: Cross-statement invariants hold after merge")
    print("=" * 70)

    periods = ["2024"]

    # Build Assets tree: declared TA=1000, children sum=950, gap=50
    cash = build_test_tree("Cash", 1.0, {"2024": 200})
    ar = build_test_tree("AR", 1.0, {"2024": 300})
    tca = build_test_tree("TCA", 1.0, {"2024": 500}, [cash, ar])

    ppe = build_test_tree("PPE", 1.0, {"2024": 400})
    other_nca = build_test_tree("OtherNCA", 1.0, {"2024": 50})
    tnca = build_test_tree("TNCA", 1.0, {"2024": 450}, [ppe, other_nca])

    # TA declared = 1000, children (TCA+TNCA) = 500+450 = 950, gap=50
    ta = build_test_tree("TotalAssets", 1.0, {"2024": 1000}, [tca, tnca])
    ta.role = "BS_TA"

    # Build L&E tree: TL declared=600 (children sum 580), TE declared=400 (children sum 390)
    ap = build_test_tree("AP", 1.0, {"2024": 280})
    debt_c = build_test_tree("DebtCurrent", 1.0, {"2024": 300})
    tl = build_test_tree("TotalLiabilities", 1.0, {"2024": 600}, [ap, debt_c])
    tl.role = "BS_TL"

    re_ = build_test_tree("RetainedEarnings", 1.0, {"2024": 350})
    apic = build_test_tree("APIC", 1.0, {"2024": 40})
    te = build_test_tree("TotalEquity", 1.0, {"2024": 400}, [re_, apic])
    te.role = "BS_TE"

    pres_index = {
        "Cash": 1.0, "AR": 2.0, "PPE": 3.0, "OtherNCA": 4.0,
        "AP": 1.0, "DebtCurrent": 2.0,
        "RetainedEarnings": 1.0, "APIC": 2.0,
    }

    merge_calc_pres(ta, pres_index, periods)
    merge_calc_pres(tl, pres_index, periods)
    merge_calc_pres(te, pres_index, periods)

    print("Assets tree:")
    print_merged_tree(ta, periods=periods)
    print("\nLiabilities tree:")
    print_merged_tree(tl, periods=periods)
    print("\nEquity tree:")
    print_merged_tree(te, periods=periods)

    # After merge, each tree's formula equals its declared value
    # So TA_formula=1000, TL_formula=600, TE_formula=400
    # BS Balance check: 1000 - 600 - 400 = 0 ✓

    for tree_node, label in [(ta, "Assets"), (tl, "Liabilities"), (te, "Equity")]:
        errors = verify_tree_completeness(tree_node, periods)
        assert not errors, f"{label} has gaps: {errors}"

    # Cross-statement check
    ta_val = ta.values["2024"]
    tl_val = tl.values["2024"]
    te_val = te.values["2024"]
    bs_balance = ta_val - tl_val - te_val
    assert bs_balance == 0, f"BS doesn't balance: {ta_val} - {tl_val} - {te_val} = {bs_balance}"

    print(f"\nBS Balance: {ta_val:,} - {tl_val:,} - {te_val:,} = {bs_balance}")
    print("PASS ✓")


def test_7_overshoot():
    """Children sum EXCEEDS parent (overshoot) → Other is negative."""
    print("\n" + "=" * 70)
    print("TEST 7: Overshoot → negative Other")
    print("=" * 70)

    periods = ["2024"]

    a = build_test_tree("A", 1.0, {"2024": 300})
    b = build_test_tree("B", 1.0, {"2024": 250})
    # Children sum = 550, parent declared = 500 → overshoot of 50
    parent = build_test_tree("Total", 1.0, {"2024": 500}, [a, b])

    merge_calc_pres(parent, {"A": 1.0, "B": 2.0}, periods)
    print_merged_tree(parent, periods=periods)

    errors = verify_tree_completeness(parent, periods)
    assert not errors, f"FAIL: {errors}"
    other = [c for c in parent.children if c.concept == "__OTHER__"][0]
    assert other.values["2024"] == -50, f"Expected -50, got {other.values['2024']}"
    print("PASS ✓")


def test_8_empty_pres_index():
    """No presentation data available → calc order preserved, Other still works."""
    print("\n" + "=" * 70)
    print("TEST 8: Empty pres_index → calc order preserved")
    print("=" * 70)

    periods = ["2024"]

    a = build_test_tree("A", 1.0, {"2024": 100})
    b = build_test_tree("B", 1.0, {"2024": 200})
    parent = build_test_tree("Total", 1.0, {"2024": 350}, [a, b])

    merge_calc_pres(parent, {}, periods)  # Empty pres
    print_merged_tree(parent, periods=periods)

    # Order preserved (both unpresented)
    assert parent.children[0].concept == "A"
    assert parent.children[1].concept == "B"
    # Gap = 350 - 300 = 50
    other = [c for c in parent.children if c.concept == "__OTHER__"][0]
    assert other.values["2024"] == 50
    errors = verify_tree_completeness(parent, periods)
    assert not errors
    print("PASS ✓")


def test_9_multi_period_consistency():
    """Other values are computed independently per period."""
    print("\n" + "=" * 70)
    print("TEST 9: Multi-period — Other computed per period")
    print("=" * 70)

    periods = ["2024", "2023", "2022"]

    a = build_test_tree("A", 1.0, {"2024": 100, "2023": 80, "2022": 60})
    b = build_test_tree("B", 1.0, {"2024": 200, "2023": 190, "2022": 180})
    # Gaps: 2024=50, 2023=30, 2022=10
    parent = build_test_tree("Total", 1.0,
                             {"2024": 350, "2023": 300, "2022": 250},
                             [a, b])

    merge_calc_pres(parent, {"A": 1.0, "B": 2.0}, periods)
    print_merged_tree(parent, periods=periods)

    other = [c for c in parent.children if c.concept == "__OTHER__"][0]
    assert other.values["2024"] == 50
    assert other.values["2023"] == 30
    assert other.values["2022"] == 10
    errors = verify_tree_completeness(parent, periods)
    assert not errors
    print("PASS ✓")


# ---------------------------------------------------------------------------
# Real-world tests against fixture data
# ---------------------------------------------------------------------------

def _load_fixture(company: str) -> tuple[dict, list[str]]:
    """Load a company's tree fixture. Returns (trees_dict, periods)."""
    import json
    fixture_path = f"tests/fixtures/sec_filings/{company}/trees.json"
    with open(fixture_path) as f:
        d = json.load(f)
    periods = d.get("complete_periods", [])
    # Reconstruct TreeNodes
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in d and isinstance(d[stmt], dict):
            d[stmt] = TreeNode.from_dict(d[stmt])
    return d, periods


def _count_others(tree: TreeNode) -> int:
    """Count __OTHER__ nodes in a tree."""
    count = 1 if tree.concept == "__OTHER__" else 0
    for c in tree.children:
        count += _count_others(c)
    return count


def _collect_roles(tree: TreeNode) -> dict[str, float]:
    """Collect {role: declared_value} for first period."""
    result = {}
    def _walk(node):
        if node.role and node.values:
            first_period = sorted(node.values.keys())[0] if node.values else None
            if first_period:
                result[node.role] = node.values[first_period]
        for c in node.children:
            _walk(c)
    _walk(tree)
    return result


def test_real_single_company(company: str):
    """Test merge on a single company's real data."""
    print(f"\n{'=' * 70}")
    print(f"REAL DATA: {company}")
    print("=" * 70)

    trees, periods = _load_fixture(company)

    # Count gaps BEFORE merge
    gaps_before = {}
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if not tree:
            continue
        errors = verify_tree_completeness(tree, periods)
        gaps_before[stmt] = len(errors)
        if errors:
            print(f"  {stmt} BEFORE: {len(errors)} gaps")
            for concept, period, gap in errors[:3]:
                print(f"    {concept.split('_',1)[-1][:50]:50s} {period} gap={gap:>12,.0f}")

    # Apply merge (with empty pres_index — ordering test is synthetic,
    # this test is about the math)
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            merge_calc_pres(tree, {}, periods)

    # Count gaps AFTER merge
    gaps_after = {}
    all_pass = True
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if not tree:
            continue
        errors = verify_tree_completeness(tree, periods)
        gaps_after[stmt] = len(errors)
        others = _count_others(tree)
        print(f"  {stmt} AFTER:  {len(errors)} gaps, {others} Other rows inserted")
        if errors:
            all_pass = False
            for concept, period, gap in errors:
                print(f"    REMAINING GAP: {concept.split('_',1)[-1][:50]:50s} {period} gap={gap:>12,.0f}")

    # Cross-statement invariants
    roles = {}
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            roles.update(_collect_roles(tree))

    first_period = periods[0] if periods else None
    if first_period:
        # BS Balance: TA - TL - TE
        if all(r in roles for r in ["BS_TA", "BS_TL", "BS_TE"]):
            bs_bal = roles["BS_TA"] - roles["BS_TL"] - roles["BS_TE"]
            status = "✓" if abs(bs_bal) < 1 else f"FAIL ({bs_bal:,.0f})"
            print(f"  BS Balance:  {roles['BS_TA']:>12,.0f} - {roles['BS_TL']:>12,.0f} - {roles['BS_TE']:>12,.0f} = {bs_bal:>8,.0f} {status}")

        # NI Link: INC_NET vs INC_NET_CF
        if all(r in roles for r in ["INC_NET", "INC_NET_CF"]):
            ni_gap = roles["INC_NET"] - roles["INC_NET_CF"]
            status = "✓" if abs(ni_gap) < 1 else f"FAIL ({ni_gap:,.0f})"
            print(f"  NI Link:     {roles['INC_NET']:>12,.0f} - {roles['INC_NET_CF']:>12,.0f} = {ni_gap:>8,.0f} {status}")

        # D&A Link
        if all(r in roles for r in ["IS_DA", "CF_DA"]):
            da_gap = roles["IS_DA"] - roles["CF_DA"]
            status = "✓" if abs(da_gap) < 1 else f"FAIL ({da_gap:,.0f})"
            print(f"  D&A Link:    {roles['IS_DA']:>12,.0f} - {roles['CF_DA']:>12,.0f} = {da_gap:>8,.0f} {status}")

        # SBC Link
        if all(r in roles for r in ["IS_SBC", "CF_SBC"]):
            sbc_gap = roles["IS_SBC"] - roles["CF_SBC"]
            status = "✓" if abs(sbc_gap) < 1 else f"FAIL ({sbc_gap:,.0f})"
            print(f"  SBC Link:    {roles['IS_SBC']:>12,.0f} - {roles['CF_SBC']:>12,.0f} = {sbc_gap:>8,.0f} {status}")

    assert all_pass, f"{company}: still has gaps after merge"
    print(f"  {company}: ALL GAPS CLOSED ✓")
    return True


def test_real_all_companies():
    """Run merge on all fixture companies."""
    print("\n" + "=" * 70)
    print("TEST 10: Real-world data — all fixture companies")
    print("=" * 70)

    companies = ["NFLX", "AAPL", "AMZN", "GOOG", "META", "TSLA", "JPM", "PFE"]
    results = {}
    for co in companies:
        try:
            test_real_single_company(co)
            results[co] = "PASS"
        except FileNotFoundError:
            results[co] = "SKIP (no fixture)"
        except AssertionError as e:
            results[co] = f"FAIL: {e}"

    print(f"\n{'=' * 70}")
    print("SUMMARY — Real-World Results")
    print("=" * 70)
    for co, status in results.items():
        print(f"  {co:5s}: {status}")

    passed = sum(1 for v in results.values() if v == "PASS")
    total = sum(1 for v in results.values() if v != "SKIP (no fixture)")
    print(f"\n  {passed}/{total} companies: all gaps closed by Other rows")
    assert passed == total, f"Some companies still have gaps"
    print("PASS ✓")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_1_simple_gap,
        test_2_no_gap,
        test_3_subtraction_weight,
        test_4_unpresented_children,
        test_5_nested_gaps,
        test_6_cross_statement_invariants,
        test_7_overshoot,
        test_8_empty_pres_index,
        test_9_multi_period_consistency,
        test_real_all_companies,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 70}")
    print(f"Results: {passed}/{len(tests)} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASS")
    sys.exit(failed)
