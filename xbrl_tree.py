"""
XBRL Calculation Tree Builder
==============================
Parses the XBRL calculation linkbase (_cal.xml) to build a tree of
parent-child relationships with weights (+1/-1), then attaches actual
values from iXBRL facts.

The tree structure defines which items are:
- Parent-child (subtraction): structural, never group
- Siblings under same parent (addition): can group small ones into "Other"

Usage:
  python xbrl_tree.py --html filing.htm -o tree.json
  python xbrl_tree.py --html filing.htm --print
"""

import argparse
import json
import sys

from sec_utils import fetch_url
from xbrl import *
from xbrl.tree import TreeNode, _concept_to_tag, _clean_name, build_tree, find_roots, find_groupable_siblings, build_presentation_index, sort_by_presentation, cascade_layout, print_tree, find_node_by_role, _find_parent, _supplement_orphan_facts, _supplement_orphan_facts_all, _filter_to_complete_periods
from xbrl.linkbase import fetch_cal_linkbase, fetch_pre_linkbase, fetch_lab_linkbase, parse_lab_linkbase, get_label, parse_pre_linkbase, parse_calc_linkbase, classify_roles, STATEMENT_ROLE_PATTERNS
from xbrl.segments import _find_best_decomposition, _detect_segments_for_node, _attach_segment_children, _attach_is_segments, _build_revenue_segment_tree
from xbrl.reconcile import CROSS_STATEMENT_CHECKS, _tag_bs_positions, _find_by_keywords, _tag_is_semantic, merge_calc_pres, _find_leaf_by_timeseries, _tag_da_sbc_nodes, _tag_cf_positions, _tag_is_positions, _override_bs_cash, verify_tree_completeness

def main():
    parser = argparse.ArgumentParser(description="Build XBRL calculation tree")
    parser.add_argument("--html", help="Path to filing HTML file")
    parser.add_argument("--url", help="URL to filing HTML")
    parser.add_argument("-o", "--output", help="Output JSON file")
    parser.add_argument("--print", dest="do_print", action="store_true",
                        help="Print tree to stdout")
    args = parser.parse_args()

    if args.url:
        html = fetch_url(args.url).decode('utf-8', errors='replace')
        base_url = args.url.rsplit('/', 1)[0] + '/'
    elif args.html:
        with open(args.html) as f:
            html = f.read()
        base_url = ""
    else:
        print("Error: provide --url or --html", file=sys.stderr)
        sys.exit(1)

    result = build_statement_trees(html, base_url)
    if not result:
        sys.exit(1)

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = result.get(stmt)
        if not tree:
            continue

        if args.do_print:
            label = {"IS": "INCOME STATEMENT", "BS": "BALANCE SHEET (Assets)",
                     "BS_LE": "BALANCE SHEET (Liabilities + Equity)",
                     "CF": "CASH FLOWS"}[stmt]
            print(f"\n{'=' * 80}")
            print(f"{label}")
            print(f"{'=' * 80}")
            print_tree(tree)

            groups = find_groupable_siblings(tree)
            if groups:
                print(f"\n  Groupable sibling sets ({len(groups)}):")
                for g in groups:
                    print(f"    Under {g['parent_name']}: {len(g['siblings'])} siblings")
                    for s in g['siblings']:
                        avg = sum(abs(v) for v in s['values'].values()) / max(len(s['values']), 1)
                        print(f"      {s['name']:40s}  avg|val|={avg:>12,.0f}")

    if args.output:
        out = {}
        for key in ["complete_periods", "periods", "cf_endc_values", "unit_label"]:
            if key in result:
                out[key] = result[key]
        out["facts"] = result.get("facts", {})
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = result.get(stmt)
            if tree:
                out[stmt] = tree.to_dict()
                out[f"{stmt}_groupable"] = find_groupable_siblings(tree)
        rev_seg = result.get("revenue_segments")
        if rev_seg:
            out["revenue_segments"] = rev_seg.to_dict()
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
