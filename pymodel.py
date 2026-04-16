import argparse
import json
import sys

from xbrl_tree import (
    TreeNode,
    find_node_by_role,
    build_statement_trees,
    reconcile_trees,
)


def verify_model(trees: dict) -> list[tuple]:
    """Run 7 cross-statement invariant checks on reconciled trees using fv().

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
    periods = sorted(trees.get("complete_periods", []))

    # Locate nodes by role
    bs_ta = find_node_by_role(trees["BS"], "BS_TA") if trees.get("BS") else None
    bs_tl = find_node_by_role(trees["BS_LE"], "BS_TL") if trees.get("BS_LE") else None
    bs_te = find_node_by_role(trees["BS_LE"], "BS_TE") if trees.get("BS_LE") else None
    bs_cash = find_node_by_role(trees["BS"], "BS_CASH") if trees.get("BS") else None
    inc_net_is = find_node_by_role(trees["IS"], "INC_NET") if trees.get("IS") else None
    inc_net_cf = (
        find_node_by_role(trees["CF"], "INC_NET_CF") if trees.get("CF") else None
    )

    is_da = find_node_by_role(trees["IS"], "IS_DA") if trees.get("IS") else None
    cf_da = find_node_by_role(trees["CF"], "CF_DA") if trees.get("CF") else None
    is_sbc = find_node_by_role(trees["IS"], "IS_SBC") if trees.get("IS") else None
    cf_sbc = find_node_by_role(trees["CF"], "CF_SBC") if trees.get("CF") else None
    cf_begc = find_node_by_role(trees["CF"], "CF_BEGC") if trees.get("CF") else None

    def fv(node, period):
        """Formula value: what =SUM(children) would produce in the sheet.
        Falls back to declared value for leaves."""
        if node is None:
            return 0
        if not node.children:
            return node.values.get(period, 0)
        return sum(fv(c, period) * c.weight for c in node.children)

    # Use CF_ENDC values computed by xbrl_tree.py (single source of truth).
    # Falls back to facts lookup if cf_endc_values not stored.
    cf_endc_values = trees.get("cf_endc_values", {})
    if not cf_endc_values:
        facts = trees.get("facts", {})
        for tag in [
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        ]:
            if tag in facts:
                cf_endc_values = facts[tag]
                break

    def check(name, period, val):
        if abs(val) > 1.0:
            errors.append((name, period, val))

    for p in periods:
        # 1. BS Balance: TA == TL + TE
        if bs_ta and bs_tl and bs_te:
            check(
                "BS Balance (TA-TL-TE)", p, fv(bs_ta, p) - fv(bs_tl, p) - fv(bs_te, p)
            )

        # 2. Cash Link: CF_ENDC == BS_CASH
        if bs_cash and cf_endc_values:
            cf_endc = cf_endc_values.get(p, 0)
            if cf_endc != 0:
                check("Cash (CF_ENDC - BS_CASH)", p, cf_endc - fv(bs_cash, p))

        # 3. NI Link: INC_NET (IS) == INC_NET (CF)
        if inc_net_is and inc_net_cf:
            is_ni = fv(inc_net_is, p)
            cf_ni = fv(inc_net_cf, p)
            if is_ni != 0:
                check("NI Link (IS - CF)", p, is_ni - cf_ni)

        # Check 4: D&A Link (role-tag-based)
        if is_da and cf_da:
            is_da_val = fv(is_da, p)
            cf_da_val = fv(cf_da, p)
            if is_da_val != 0:
                check("D&A Link (IS - CF)", p, is_da_val - cf_da_val)

        # Check 5: SBC Link (role-tag-based)
        if is_sbc and cf_sbc:
            is_sbc_val = fv(is_sbc, p)
            cf_sbc_val = fv(cf_sbc, p)
            if is_sbc_val != 0:
                check("SBC Link (IS - CF)", p, is_sbc_val - cf_sbc_val)

        # Check 6: Cash Begin: CF_BEGC[t] == BS_CASH[t-1]
        if cf_begc and bs_cash and len(periods) > 1:
            p_idx = periods.index(p)
            if p_idx > 0:
                prev_p = periods[p_idx - 1]
                begc_val = fv(cf_begc, p)
                bs_cash_prev = fv(bs_cash, prev_p)
                if begc_val != 0 and bs_cash_prev != 0:
                    check(
                        "Cash Begin (CF_BEGC - BS_CASH[t-1])",
                        p,
                        begc_val - bs_cash_prev,
                    )

    # Check 7: Segment sums
    is_rev = find_node_by_role(trees["IS"], "IS_REVENUE") if trees.get("IS") else None
    is_cogs = find_node_by_role(trees["IS"], "IS_COGS") if trees.get("IS") else None
    for label, node in [("IS Revenue", is_rev), ("IS COGS", is_cogs)]:
        if node and node.children:
            _verify_segment_sums(node, periods, errors, label_prefix=label)

    return errors


def _verify_segment_sums(
    node: TreeNode, periods: list[str], errors: list, label_prefix: str = "Segments"
):
    """Recursively verify that children sum to parent at every level.

    Uses fv() (formula values) for children, so the check reflects what
    =SUM(children) would actually produce in the sheet.
    """

    def _fv(n, period):
        """Formula value: what =SUM(children) would produce."""
        if not n.children:
            return n.values.get(period, 0)
        return sum(_fv(c, period) * c.weight for c in n.children)

    if not node.children:
        return
    for p in periods:
        parent_val = node.values.get(p, 0)
        children_sum = sum(_fv(c, p) * c.weight for c in node.children)
        delta = parent_val - children_sum
        if abs(delta) > 1.0:
            errors.append((f"{label_prefix} ({node.name})", p, delta))
    for child in node.children:
        _verify_segment_sums(child, periods, errors, label_prefix=label_prefix)


class CheckpointResult:
    """Structured result from cross-statement invariant verification."""

    def __init__(self, passed: bool, errors: list[tuple], periods: list[str]):
        self.passed = passed
        self.errors = errors
        self.periods = periods

    @property
    def first_error(self) -> str | None:
        if self.errors:
            name, period, delta = self.errors[0]
            return f"{name} for {period} (gap=${delta:,.0f})"
        return None


def run_checkpoint(trees_data: dict) -> CheckpointResult:
    """Run cross-statement invariant checks with LLM fix attempt.

    Unlike main(), this does NOT call sys.exit. Returns a structured result.
    Safe to call from the demo's worker thread.
    """
    errors = verify_model(trees_data)

    if errors:
        from llm_invariant_fixer import fix_invariants

        print(
            f"verify_model initially found {len(errors)} error(s), attempting LLM fix...",
            file=sys.stderr,
        )
        if fix_invariants(trees_data):
            errors = verify_model(trees_data)

    periods = trees_data.get("complete_periods", [])
    return CheckpointResult(passed=len(errors) == 0, errors=errors, periods=periods)


def main():
    parser = argparse.ArgumentParser(description="Verify financial model invariants")
    parser.add_argument("--trees", required=True, help="Path to reconciled trees JSON")
    parser.add_argument(
        "--checkpoint",
        action="store_true",
        help="Run verification and exit (no output)",
    )
    args = parser.parse_args()

    with open(args.trees) as f:
        trees_data = json.load(f)

    result = run_checkpoint(trees_data)

    if result.errors:
        print(f"verify_model: {len(result.errors)} error(s)", file=sys.stderr)
        for name, period, delta in result.errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
        sys.exit(1)
    else:
        n = len(result.periods)
        print(f"verify_model: ALL PASS ({n} periods)", file=sys.stderr)


if __name__ == "__main__":
    main()
