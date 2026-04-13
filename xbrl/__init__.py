import sys
from .tree import TreeNode, build_tree, find_roots, _filter_to_complete_periods, build_presentation_index
from .linkbase import fetch_cal_linkbase, fetch_pre_linkbase, fetch_lab_linkbase, parse_calc_linkbase, parse_pre_linkbase, parse_lab_linkbase, classify_roles
from .reconcile import _tag_bs_positions, _tag_cf_positions, _tag_is_positions, _tag_is_semantic, _override_bs_cash, merge_calc_pres, _tag_da_sbc_nodes
from .segments import _attach_is_segments, _build_revenue_segment_tree

from parse_xbrl_facts import build_xbrl_facts_dict, build_segment_facts_dict

__all__ = ["TreeNode", "reconcile_trees", "build_statement_trees"]

def reconcile_trees(trees: dict, pres_index: dict | None = None) -> dict:
    facts = trees.get("facts", {})
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)
    _tag_is_positions(trees.get("IS"), trees.get("CF"))
    _tag_is_semantic(trees.get("IS"))
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    if cf_endc_values:
        trees["cf_endc_values"] = cf_endc_values

    _filter_to_complete_periods(trees)

    periods = trees.get("complete_periods", [])
    if periods:
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if tree:
                stmt_pres = pres_index.get(stmt, {}) if pres_index else {}
                merge_calc_pres(tree, stmt_pres, periods)

    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    return trees

def build_statement_trees(html: str, base_url: str) -> dict:
    facts, unit_label = build_xbrl_facts_dict(html)
    cal_xml = fetch_cal_linkbase(html, base_url)
    if not cal_xml:
        print("ERROR: Could not find calculation linkbase", file=sys.stderr)
        return None

    all_trees = parse_calc_linkbase(cal_xml)
    stmt_roles = classify_roles(list(all_trees.keys()))

    result = {"facts": facts, "unit_label": unit_label}
    for stmt, role in stmt_roles.items():
        calc_children = all_trees[role]
        roots = find_roots(calc_children)
        best_root = roots[0] if roots else None
        for r in roots:
            rl = r.lower()
            if stmt == "IS" and "netincomeloss" in rl and "available" not in rl:
                best_root = r
            elif stmt == "BS" and r.endswith("Assets"):
                best_root = r
            elif stmt == "CF" and "cashequivalents" in rl:
                best_root = r

        if best_root:
            tree = build_tree(calc_children, facts, best_root)
            result[stmt] = tree

        if stmt == "BS":
            for r in roots:
                if r != best_root:
                    tree2 = build_tree(calc_children, facts, r)
                    result["BS_LE"] = tree2

    pre_xml = fetch_pre_linkbase(html, base_url)
    role_orders = parse_pre_linkbase(pre_xml) if pre_xml else {}

    pres_index = {}
    for stmt, role in stmt_roles.items():
        pres_index[stmt] = build_presentation_index(role_orders, role)
        if stmt == "BS":
            pres_index["BS_LE"] = pres_index["BS"]

    all_periods = set()
    for tag_vals in facts.values():
        all_periods.update(tag_vals.keys())
    result["periods"] = sorted(all_periods)

    reconcile_trees(result, pres_index)

    seg_facts, multi_seg_facts = build_segment_facts_dict(html)
    lab_xml = fetch_lab_linkbase(html, base_url)
    lab_labels = parse_lab_linkbase(lab_xml) if lab_xml else {}
    result["lab_labels"] = lab_labels
    _attach_is_segments(result, seg_facts, lab_labels)

    rev_segments = _build_revenue_segment_tree(result, seg_facts, multi_seg_facts, lab_labels)
    if rev_segments:
        result["revenue_segments"] = rev_segments

    return result
