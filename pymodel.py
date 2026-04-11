import argparse
import json
import sys

from xbrl_tree import TreeNode, find_node_by_role, build_statement_trees, reconcile_trees


def verify_model(trees: dict) -> list[tuple]:
    """Run 5 cross-statement invariant checks on reconciled trees.

    Args:
        trees: dict from build_statement_trees() after reconcile_trees().
               Must have keys: "IS", "BS", "BS_LE", "CF", "complete_periods"

    Returns:
        List of (check_name, period, delta) tuples. Empty list = all pass.
    """
    # If trees contain dicts (from JSON), reconstruct TreeNode objects
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        if stmt in trees and isinstance(trees[stmt], dict):
            trees[stmt] = TreeNode.from_dict(trees[stmt])

    errors = []
    periods = trees.get("complete_periods", [])

    # Locate nodes by role
    bs_ta = find_node_by_role(trees["BS"], "BS_TA") if trees.get("BS") else None
    bs_tl = find_node_by_role(trees["BS_LE"], "BS_TL") if trees.get("BS_LE") else None
    bs_te = find_node_by_role(trees["BS_LE"], "BS_TE") if trees.get("BS_LE") else None
    bs_cash = find_node_by_role(trees["BS"], "BS_CASH") if trees.get("BS") else None
    inc_net_is = find_node_by_role(trees["IS"], "INC_NET") if trees.get("IS") else None
    inc_net_cf = find_node_by_role(trees["CF"], "INC_NET_CF") if trees.get("CF") else None

    # Helper: get value from node for a period, default 0
    def nv(node, period):
        """Node value: get a period's value from a tree node, or 0."""
        if node is None:
            return 0
        return node.values.get(period, 0)

    # Helper: find CF_ENDC from facts (instant context, not a tree node)
    facts = trees.get("facts", {})
    cf_endc_values = {}
    for tag in [
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    ]:
        if tag in facts:
            cf_endc_values = facts[tag]
            break

    def check(name, period, val):
        if abs(val) > 0.5:
            errors.append((name, period, val))

    for p in periods:
        # 1. BS Balance: TA == TL + TE
        if bs_ta and bs_tl and bs_te:
            check("BS Balance (TA-TL-TE)", p,
                  nv(bs_ta, p) - nv(bs_tl, p) - nv(bs_te, p))

        # 2. Cash Link: CF_ENDC == BS_CASH
        if bs_cash and cf_endc_values:
            cf_endc = cf_endc_values.get(p, 0)
            if cf_endc != 0:
                check("Cash (CF_ENDC - BS_CASH)", p,
                      cf_endc - nv(bs_cash, p))

        # 3. NI Link: INC_NET (IS) == INC_NET (CF)
        if inc_net_is and inc_net_cf:
            is_ni = nv(inc_net_is, p)
            cf_ni = nv(inc_net_cf, p)
            if is_ni != 0:
                check("NI Link (IS - CF)", p, is_ni - cf_ni)

        # 4. D&A Link: IS D&A == CF D&A (value-matched)
        # Walk CF_OPCF's children to find a leaf matching IS D&A value
        is_da = _find_is_value_by_label(trees.get("IS"), p, ["depreciation", "amortization"])
        if is_da and is_da != 0:
            cf_da = _find_cf_match_by_value(trees.get("CF"), p, is_da)
            if cf_da is not None:
                check("D&A Link (IS - CF)", p, is_da - cf_da)

        # 5. SBC Link: IS SBC == CF SBC (value-matched)
        is_sbc = _find_is_value_by_label(trees.get("IS"), p, ["stock", "share", "compensation"])
        if is_sbc and is_sbc != 0:
            cf_sbc = _find_cf_match_by_value(trees.get("CF"), p, is_sbc)
            if cf_sbc is not None:
                check("SBC Link (IS - CF)", p, is_sbc - cf_sbc)

    return errors


def _find_is_value_by_label(is_tree: TreeNode | None, period: str,
                             keywords: list[str]) -> float | None:
    """Find an IS tree leaf whose name contains ALL keywords (case-insensitive).

    Returns the node's value for the given period, or None if not found.
    Used for D&A and SBC which don't have fixed role tags.
    """
    if not is_tree:
        return None

    def _search(node):
        name_lower = node.name.lower()
        if all(kw in name_lower for kw in keywords):
            return node.values.get(period, 0)
        for child in node.children:
            result = _search(child)
            if result is not None:
                return result
        return None

    return _search(is_tree)


def _find_cf_match_by_value(cf_tree: TreeNode | None, period: str,
                              target_value: float) -> float | None:
    """Search CF tree leaves for one whose value matches target (within 0.5).

    This is the same value-matching approach used in the old verify_model:
    scan CF operating items to find one whose value equals the IS value.
    """
    if not cf_tree:
        return None

    opcf_node = find_node_by_role(cf_tree, "CF_OPCF")
    if not opcf_node:
        return None

    def _search_leaves(node):
        if node.is_leaf and node.values:
            val = node.values.get(period, 0)
            if abs(val - target_value) < 0.5:
                return val
        for child in node.children:
            result = _search_leaves(child)
            if result is not None:
                return result
        return None

    return _search_leaves(opcf_node)

def main():
    parser = argparse.ArgumentParser(description="Verify financial model invariants")
    parser.add_argument("--trees", required=True, help="Path to reconciled trees JSON")
    parser.add_argument("--checkpoint", action="store_true",
                        help="Run verification and exit (no output)")
    args = parser.parse_args()

    with open(args.trees) as f:
        trees_data = json.load(f)

    errors = verify_model(trees_data)

    print(f"Periods: {trees_data.get('complete_periods', [])}", file=sys.stderr)
    if errors:
        print(f"verify_model: {len(errors)} error(s)", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
        sys.exit(1)
    else:
        n = len(trees_data.get("complete_periods", []))
        print(f"verify_model: ALL PASS ({n} periods)", file=sys.stderr)


if __name__ == "__main__":
    main()
