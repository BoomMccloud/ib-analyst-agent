#!/usr/bin/env python3
"""
POC: Reclassification detection for TSLA Revenue concept change.

Problem:
  - Older filings (2022, 2023): IS_REVENUE = us-gaap_Revenues (parent)
    with us-gaap_RevenueFromContractWithCustomer... as empty child
  - Newer filings (2024+): IS_REVENUE = us-gaap_RevenueFromContractWithCustomer...
    (Revenues doesn't exist)
  - After merge: Revenues is the parent with values for 2020-2022 only,
    RevenueFromContract is child with values for 2022-2025.
    For 2023+, fv(Revenues) = fv(RevenueFromContract) = 96K,
    but Revenues.declared = 0 → residual absorbs 96K → NI Link breaks.

Hypothesis:
  If we detect this as a rename (same value at overlap period) and merge
  the values into one node, the residual goes away.

Approach:
  1. Load all TSLA tree files
  2. At each overlap period, check parent-child pairs where:
     - Parent has values for older periods only
     - Child has the SAME value as parent at overlap period
     - Child has values for newer periods
  3. This means: the child IS the parent in newer filings (rename/reclassification)
  4. Fix: copy parent's older-period values into the child, remove parent,
     promote child to parent's position
  5. Re-merge and verify
"""

import json
import glob
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xbrl_tree import TreeNode, find_node_by_role, reconcile_trees


def detect_parent_child_renames(tree, periods):
    """Find parent-child pairs where the child replaced the parent across filings.

    Detection: at some overlap period P, parent.values[P] == child.values[P],
    and parent has no values for periods after P, while child has values after P.
    """
    renames = []

    def _scan(node):
        if not node.children:
            return
        for child in node.children:
            if child.concept.startswith("__"):
                continue
            # Check: do parent and child share a value at any period?
            shared_periods = set(node.values.keys()) & set(child.values.keys())
            for p in shared_periods:
                if abs(node.values[p] - child.values[p]) < 1.0 and node.values[p] != 0:
                    # Same value at overlap period — potential rename
                    # Check: does child have values the parent doesn't?
                    child_only = set(child.values.keys()) - set(node.values.keys())
                    parent_only = set(node.values.keys()) - set(child.values.keys())
                    if child_only:
                        renames.append({
                            "parent_concept": node.concept,
                            "child_concept": child.concept,
                            "overlap_period": p,
                            "shared_value": node.values[p],
                            "parent_only_periods": sorted(parent_only),
                            "child_only_periods": sorted(child_only),
                            "parent_node": node,
                            "child_node": child,
                        })
            # Recurse
            _scan(child)

    _scan(tree)
    return renames


def apply_rename_fix(tree, renames):
    """For each detected rename, merge parent values into child and remove parent.

    Strategy: The child IS the concept going forward. Copy the parent's
    older-period values into the child. Then replace the parent with the child
    in the grandparent's children list.
    """
    for r in renames:
        parent = r["parent_node"]
        child = r["child_node"]

        print(f"  Rename: {parent.concept} → {child.concept}")
        print(f"    Overlap: {r['overlap_period']} = {r['shared_value']}")
        print(f"    Parent-only periods: {r['parent_only_periods']}")
        print(f"    Child-only periods: {r['child_only_periods']}")

        # Copy parent's values into child (for periods child doesn't have)
        for period in r["parent_only_periods"]:
            if period not in child.values:
                child.values[period] = parent.values[period]

        # Copy parent's role to child if parent has one
        if parent.role and not child.role:
            child.role = parent.role

        # Move child up: find grandparent, replace parent with child
        _replace_in_tree(tree, parent, child)

    return tree


def _replace_in_tree(root, old_node, new_node):
    """Replace old_node with new_node in the tree."""
    for i, child in enumerate(root.children):
        if child is old_node:
            # Move any other children of old_node to new_node
            for oc in old_node.children:
                if oc is not new_node and not oc.concept.startswith("__"):
                    new_node.add_child(oc)
            root.children[i] = new_node
            return True
        if _replace_in_tree(child, old_node, new_node):
            return True
    return False


def main():
    # Load all TSLA tree files
    files = sorted(glob.glob("pipeline_output/validation/TSLA/trees_*.json"))
    print(f"Loading {len(files)} TSLA tree files...")

    all_data = []
    for f in files:
        with open(f) as fh:
            all_data.append((f, json.load(fh)))

    # Step 1: Run the normal merge first to get the broken merged tree
    print("\n=== Running normal merge ===")
    import subprocess
    result = subprocess.run(
        [sys.executable, "merge_trees.py"] + files + ["-o", "/tmp/tsla_merged_before.json"],
        capture_output=True, text=True
    )

    # Check before
    result = subprocess.run(
        [sys.executable, "pymodel.py", "--trees", "/tmp/tsla_merged_before.json", "--checkpoint"],
        capture_output=True, text=True
    )
    print("BEFORE fix:")
    for line in result.stderr.strip().split("\n"):
        print(f"  {line}")

    # Step 2: Load the merged tree and detect renames
    print("\n=== Detecting parent-child renames ===")
    with open("/tmp/tsla_merged_before.json") as f:
        merged = json.load(f)

    periods = merged.get("complete_periods", [])

    for stmt in ["IS", "BS", "CF"]:
        if stmt not in merged or not merged[stmt]:
            continue
        tree = TreeNode.from_dict(merged[stmt])
        renames = detect_parent_child_renames(tree, periods)
        if renames:
            print(f"\n{stmt} tree: {len(renames)} rename(s) detected")
            tree = apply_rename_fix(tree, renames)
            merged[stmt] = tree.to_dict()

    # Step 3: Recompute residuals after the fix
    # We need to recompute __OTHER__ nodes since the tree structure changed
    print("\n=== Recomputing residuals ===")
    for stmt in ["IS", "BS", "CF"]:
        if stmt not in merged or not merged[stmt]:
            continue
        tree = TreeNode.from_dict(merged[stmt])
        _recompute_residuals(tree, periods)
        merged[stmt] = tree.to_dict()

    # Step 4: Save and verify
    with open("/tmp/tsla_merged_after.json", "w") as f:
        json.dump(merged, f, indent=2)

    result = subprocess.run(
        [sys.executable, "pymodel.py", "--trees", "/tmp/tsla_merged_after.json", "--checkpoint"],
        capture_output=True, text=True
    )
    print("\nAFTER fix:")
    for line in result.stderr.strip().split("\n"):
        print(f"  {line}")

    if result.returncode == 0:
        print("\n✓ POC SUCCEEDS — reclassification detection fixes the TSLA NI Link error")
    else:
        print("\n✗ POC FAILS — reclassification detection alone doesn't fix it")
        print("  Remaining errors need further investigation")


def _recompute_residuals(tree, periods):
    """Recompute __OTHER__ residual nodes for all parents."""
    def _recompute(node):
        if not node.children:
            return

        # Recurse first (bottom-up)
        for child in node.children:
            _recompute(child)

        # Find or create __OTHER__ node
        other = None
        real_children = []
        for child in node.children:
            if child.concept.startswith("__OTHER__"):
                other = child
            else:
                real_children.append(child)

        # Compute residual for each period
        residuals = {}
        for p in periods:
            parent_val = node.values.get(p, 0)
            if parent_val == 0:
                continue
            children_sum = sum(
                _fv(c, p) * c.weight for c in real_children
            )
            residual = parent_val - children_sum
            if abs(residual) > 0.5:
                residuals[p] = residual

        if residuals:
            if other is None:
                other = TreeNode(f"__OTHER__{node.concept}", 1.0)
                node.add_child(other)
            other.values = residuals
        elif other is not None:
            node.children = [c for c in node.children if c is not other]

    _recompute(tree)


def _fv(node, period):
    """Formula value."""
    if not node.children:
        return node.values.get(period, 0)
    return sum(_fv(c, period) * c.weight for c in node.children)


if __name__ == "__main__":
    main()
