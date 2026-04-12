"""Merge multiple filing trees into one with full historical data.

Strategy: union of all line items across all filings.
1. Collect every concept+values from every filing
2. Use newest filing's tree structure as the skeleton
3. Value-match renamed concepts across adjacent filings
4. Add orphan concepts (only in older filings) under their parent
5. Result: one tree with all line items, all periods
"""
import json
import logging
import sys
from xbrl_tree import TreeNode

logger = logging.getLogger(__name__)


def _find_by_concept(node, concept):
    """Find a node by concept name anywhere in the tree."""
    if node.concept == concept:
        return node
    for c in node.children:
        r = _find_by_concept(c, concept)
        if r:
            return r
    return None


def _collect_all_concepts(tree, period_values=None):
    """Collect {concept: {period: value}} from a tree, recursively."""
    if period_values is None:
        period_values = {}
    if tree.concept not in period_values:
        period_values[tree.concept] = {}
    for p, v in tree.values.items():
        if v != 0:
            period_values[tree.concept][p] = v
    for c in tree.children:
        _collect_all_concepts(c, period_values)
    return period_values


def _build_concept_to_parent(tree, mapping=None):
    """Build {child_concept: parent_concept} mapping."""
    if mapping is None:
        mapping = {}
    for c in tree.children:
        mapping[c.concept] = tree.concept
        _build_concept_to_parent(c, mapping)
    return mapping


def _build_value_index(tree, period):
    """Build {value: [node]} index for a tree at a given period."""
    index = {}
    def _walk(n):
        val = n.values.get(period, 0)
        if val != 0:
            index.setdefault(val, []).append(n)
        for c in n.children:
            _walk(c)
    _walk(tree)
    return index


def _build_rename_map(base_tree, old_tree, overlap_period):
    """Build {old_concept: new_concept} mapping using value matching.

    For concepts in old_tree that don't exist in base_tree,
    find a unique value match in base_tree for the overlap period.
    """
    renames = {}
    base_index = _build_value_index(base_tree, overlap_period)
    base_concepts = set()
    def _collect(n):
        base_concepts.add(n.concept)
        for c in n.children:
            _collect(c)
    _collect(base_tree)

    def _walk_old(n):
        if n.concept not in base_concepts and n.concept != "__OTHER__":
            val = n.values.get(overlap_period, 0)
            if val != 0:
                candidates = base_index.get(val, [])
                if len(candidates) == 1:
                    renames[n.concept] = candidates[0].concept
        for c in n.children:
            _walk_old(c)
    _walk_old(old_tree)
    return renames


def _merge_values_by_concept(base_node, all_values, renames):
    """Fill in base_node's values from the all_values dict, handling renames.

    Skips __OTHER__ nodes — they are synthetic residuals that must be
    recomputed after all real concepts are filled in.
    """
    concept = base_node.concept
    if not concept.startswith("__"):
        # Direct concept match
        if concept in all_values:
            for p, v in all_values[concept].items():
                if p not in base_node.values:
                    base_node.values[p] = v
        # Check if any old concept renames to this one
        for old_concept, new_concept in renames.items():
            if new_concept == concept and old_concept in all_values:
                for p, v in all_values[old_concept].items():
                    if p not in base_node.values:
                        base_node.values[p] = v
    # Recurse
    for c in base_node.children:
        _merge_values_by_concept(c, all_values, renames)


def _find_orphans(all_values, base_tree, renames, parent_maps):
    """Find concepts that exist in older filings but not in the base tree.

    Returns {parent_concept: [(orphan_concept, {period: value})]}
    """
    # Collect all concepts in base tree
    base_concepts = set()
    def _collect(n):
        base_concepts.add(n.concept)
        for c in n.children:
            _collect(c)
    _collect(base_tree)

    # Also exclude renamed concepts (they're already mapped)
    renamed_old = set(renames.keys())
    renamed_new = set(renames.values())

    orphans = {}  # parent_concept -> [(concept, values)]
    for concept, values in all_values.items():
        if concept in base_concepts:
            continue
        if concept in renamed_old:
            continue
        if concept.startswith("__"):
            continue
        if "Member" in concept:
            continue
        # Find parent from any filing's parent map
        parent = None
        for pmap in parent_maps:
            if concept in pmap:
                parent = pmap[concept]
                # If parent was renamed, use new name
                parent = renames.get(parent, parent)
                break
        if parent and parent in base_concepts:
            orphans.setdefault(parent, []).append((concept, values))

    return orphans


def _recompute_residuals(node, periods):
    """Recompute or create __OTHER__ nodes so children always sum to parent.

    For each parent node, ensures:
      sum(children) == parent declared value
    by adjusting/creating an __OTHER__ residual child.
    """
    # Recurse bottom-up
    for c in list(node.children):
        _recompute_residuals(c, periods)

    if not node.children or not node.values:
        return

    # Find existing __OTHER__ child
    other_child = None
    for c in node.children:
        if c.concept.startswith("__"):
            other_child = c
            break

    # Compute residuals
    real_children = [c for c in node.children if not c.concept.startswith("__")]
    new_values = {}
    for p in periods:
        parent_val = node.values.get(p, 0)
        if parent_val == 0:
            continue
        real_sum = sum(c.values.get(p, 0) * c.weight for c in real_children)
        residual = parent_val - real_sum
        if abs(residual) > 0.5:
            new_values[p] = residual

    if new_values:
        if other_child:
            other_child.values = new_values
        else:
            # Create new __OTHER__ node
            other_child = TreeNode(f"__OTHER__{node.concept}", weight=1.0)
            other_child.name = "Other"
            other_child.values = new_values
            other_child.is_leaf = True
            node.add_child(other_child)

        # Sanity check: warn if residual is larger than sibling average
        for p, residual_val in new_values.items():
            residual_abs = abs(residual_val)
            child_abs_values = [abs(c.values.get(p, 0)) for c in real_children]
            if child_abs_values:
                sibling_avg = sum(child_abs_values) / len(child_abs_values)
                if residual_abs > sibling_avg:
                    logger.warning(
                        "Large residual for %s period %s: "
                        "residual=%.0f, sibling_avg=%.0f",
                        node.concept, p, residual_abs, sibling_avg
                    )
    elif other_child:
        # No residual needed — remove __OTHER__
        node.children.remove(other_child)


def merge_filing_trees(tree_files):
    """Merge multiple filing tree JSONs (newest first) into a single tree dict."""
    all_data = []
    for tf in tree_files:
        with open(tf) as f:
            all_data.append(json.load(f))

    base = all_data[0]

    # Reconstruct all trees
    filing_trees = {}  # {filing_idx: {stmt: TreeNode}}
    for i, data in enumerate(all_data):
        filing_trees[i] = {}
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            if stmt in data and isinstance(data[stmt], dict):
                filing_trees[i][stmt] = TreeNode.from_dict(data[stmt])
        if "revenue_segments" in data and isinstance(data["revenue_segments"], dict):
            filing_trees[i]["revenue_segments"] = TreeNode.from_dict(data["revenue_segments"])

    all_periods = set()
    for data in all_data:
        all_periods.update(data.get("complete_periods", []))
    all_periods = sorted(all_periods)

    for stmt in ["IS", "BS", "BS_LE", "CF", "revenue_segments"]:
        base_tree = filing_trees[0].get(stmt)
        if not base_tree:
            continue

        # Pass 1: Collect all concepts+values from all filings
        all_values = {}
        parent_maps = []
        for i in range(len(all_data)):
            old_tree = filing_trees[i].get(stmt)
            if not old_tree:
                continue
            _collect_all_concepts(old_tree, all_values)
            parent_maps.append(_build_concept_to_parent(old_tree))

        # Pass 2: Build rename maps using overlapping periods
        all_renames = {}
        for i in range(1, len(all_data)):
            old_tree = filing_trees[i].get(stmt)
            if not old_tree:
                continue
            older_periods = all_data[i].get("complete_periods", [])
            # Find overlap with the filing before it (i-1)
            prev_periods = all_data[i-1].get("complete_periods", [])
            overlap = set(prev_periods) & set(older_periods)
            if not overlap:
                continue
            overlap_period = max(overlap)
            prev_tree = filing_trees[i-1].get(stmt)
            if prev_tree:
                renames = _build_rename_map(prev_tree, old_tree, overlap_period)
                # Chain renames: if A→B and B→C, then A→C
                for old_c, new_c in renames.items():
                    final = new_c
                    while final in all_renames:
                        final = all_renames[final]
                    all_renames[old_c] = final
                if renames:
                    print(f"  {stmt}: renames at {overlap_period}: "
                          f"{', '.join(f'{k.split(chr(95),1)[-1][:30]}→{v.split(chr(95),1)[-1][:30]}' for k,v in renames.items())}",
                          file=sys.stderr)

        # Pass 3: Fill values into base tree (concept match + renames)
        _merge_values_by_concept(base_tree, all_values, all_renames)

        # Pass 4: Find orphan concepts and add them if they reduce the parent gap
        orphans = _find_orphans(all_values, base_tree, all_renames, parent_maps)
        for parent_concept, orphan_list in orphans.items():
            parent_node = _find_by_concept(base_tree, parent_concept)
            if not parent_node or not parent_node.values:
                continue
            for orphan_concept, orphan_values in orphan_list:
                filtered_vals = {p: v for p, v in orphan_values.items() if p in set(all_periods)}
                if not any(v != 0 for v in filtered_vals.values()):
                    continue
                # Only add if it reduces the gap between parent declared and children sum
                # for at least one period, and doesn't make it worse for any period
                helps = False
                hurts = False
                for p in all_periods:
                    parent_val = parent_node.values.get(p, 0)
                    if parent_val == 0:
                        continue
                    real_children = [c for c in parent_node.children if not c.concept.startswith("__")]
                    current_sum = sum(c.values.get(p, 0) * c.weight for c in real_children)
                    current_gap = abs(parent_val - current_sum)
                    new_sum = current_sum + filtered_vals.get(p, 0)
                    new_gap = abs(parent_val - new_sum)
                    if new_gap < current_gap - 0.5:
                        helps = True
                    elif new_gap > current_gap + 0.5:
                        hurts = True
                if helps and not hurts:
                    child = TreeNode(orphan_concept, weight=1.0)
                    child.values = filtered_vals
                    child.is_leaf = True
                    parent_node.add_child(child)
                    print(f"  {stmt}: added orphan {orphan_concept.split('_',1)[-1][:40]} "
                          f"under {parent_concept.split('_',1)[-1][:40]}", file=sys.stderr)
                else:
                    print(f"  {stmt}: skipped orphan {orphan_concept.split('_',1)[-1][:40]} "
                          f"(would {'hurt' if hurts else 'not help'})", file=sys.stderr)

        # Pass 5: Recompute __OTHER__ residuals
        _recompute_residuals(base_tree, all_periods)

        # Update base
        if stmt == "revenue_segments":
            base["revenue_segments"] = base_tree.to_dict()
        else:
            base[stmt] = base_tree.to_dict()

    # Merge cf_endc_values
    for data in all_data[1:]:
        for p, v in data.get("cf_endc_values", {}).items():
            if p not in base.get("cf_endc_values", {}):
                base.setdefault("cf_endc_values", {})[p] = v

    base["complete_periods"] = all_periods
    return base


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Merge multiple filing trees")
    parser.add_argument("files", nargs="+", help="Tree JSON files (newest first)")
    parser.add_argument("-o", "--output", required=True, help="Output merged JSON")
    args = parser.parse_args()

    merged = merge_filing_trees(args.files)
    with open(args.output, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"Merged {len(args.files)} filings → {args.output}")
    print(f"Periods: {merged['complete_periods']}")
