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
from concept_matcher import ConceptMatcher, ConceptMap

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


def _find_orphans(cmap: ConceptMap, base_tree):
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
    renamed_old = set(cmap.renames.keys())
    renamed_new = set(cmap.renames.values())

    orphans = {}  # parent_concept -> [(concept, values)]
    for concept, values in cmap.all_values.items():
        if concept in base_concepts:
            continue
        if concept in renamed_old:
            continue
        if concept.startswith("__OTHER__"):
            continue
        if "Member" in concept:
            continue
        # Find parent from any filing's parent map
        parent = None
        for pmap in cmap.parent_maps:
            if concept in pmap:
                parent = pmap[concept]
                # If parent was renamed, use new name
                parent = cmap.renames.get(parent, parent)
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
    other_child = next(
        (c for c in node.children if c.concept.startswith("__OTHER__")),
        None,
    )

    # Compute residuals
    real_children = [c for c in node.children if not c.concept.startswith("__OTHER__")]
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
    
    matcher = ConceptMatcher()

    for stmt in ["IS", "BS", "BS_LE", "CF", "revenue_segments"]:
        base_tree = filing_trees[0].get(stmt)
        if not base_tree:
            continue

        # Pass 1 & 2: Alignment
        cmap = matcher.align_statement(stmt, filing_trees, all_data)

        # Pass 3: Fill values into base tree
        matcher.merge_values_by_concept(base_tree, cmap)

        # Pass 4: Find orphan concepts and add them if they reduce the parent gap
        orphans = _find_orphans(cmap, base_tree)
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
                    real_children = [c for c in parent_node.children if not c.concept.startswith("__OTHER__")]
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

        # Pass 4b: Detect and fix structural reclassifications
        matcher.detect_and_fix_structural_shifts(base_tree, all_periods, stmt)

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