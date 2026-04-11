"""
XBRL Sibling Grouper
=====================
Takes the calculation tree from xbrl_tree.py, identifies groupable sibling
sets, and asks the LLM to decide which items are material enough for their
own line vs. getting lumped into "Other".

The LLM ONLY groups additive siblings. It never touches parent-child
(subtraction) relationships.

Usage:
  python xbrl_group.py --url <filing_url> --print
  python xbrl_group.py --url <filing_url> -o trees.json
"""

import argparse
import json
import sys

from anthropic import Anthropic

from xbrl_tree import (
    build_statement_trees, find_groupable_siblings, TreeNode, print_tree,
)
from sec_utils import fetch_url

HAIKU = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# LLM Grouping
# ---------------------------------------------------------------------------

def group_siblings_with_llm(client: Anthropic, groups: list[dict],
                             statement: str) -> list[dict]:
    """Ask the LLM to group small siblings within each sibling set.

    Args:
        client: Anthropic client
        groups: list of groupable sibling sets from find_groupable_siblings()
        statement: "IS", "BS", or "CF" for context

    Returns: list of decisions, one per group:
        {"parent": concept,
         "keep": [concept, ...],    # items that get their own line
         "group_into_other": [concept, ...]}  # items lumped into "Other"
    """
    decisions = []

    for group in groups:
        parent_name = group["parent_name"]
        siblings = group["siblings"]

        # Build the prompt
        items_desc = []
        for s in siblings:
            avg = sum(abs(v) for v in s["values"].values()) / max(len(s["values"]), 1)
            items_desc.append(f'  - "{s["name"]}" (avg absolute value: {avg:,.0f})')
        items_text = "\n".join(items_desc)

        parent_total_avg = 0
        if group["parent_total"]:
            parent_total_avg = sum(abs(v) for v in group["parent_total"].values()) / max(len(group["parent_total"]), 1)

        prompt = f"""You are building a financial model. Under "{parent_name}" ({statement}), these {len(siblings)} line items are additive siblings that sum to the parent total (avg ~{parent_total_avg:,.0f}):

{items_text}

Which items are material enough to deserve their own line in the model? The rest will be combined into a single "Other" line.

Rules:
- Keep items that are large (>5% of parent total) or analytically important (e.g., R&D, SBC)
- Group items that are small, volatile, or not useful for forecasting
- Keep at least 2 items and group at least 1 into "Other" (unless all are material)
- If ALL items are material, keep all (return empty group_into_other)

Return JSON only:
{{"keep": ["item name 1", "item name 2"], "group_into_other": ["item name 3"]}}"""

        response = client.messages.create(
            model=HAIKU,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # Parse JSON from response
        try:
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            result = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            # Fallback: keep top 3 by value, group rest
            keep_names = [s["name"] for s in siblings[:3]]
            other_names = [s["name"] for s in siblings[3:]]
            result = {"keep": keep_names, "group_into_other": other_names}

        # Map names back to concepts
        name_to_concept = {s["name"]: s["concept"] for s in siblings}
        keep_concepts = []
        other_concepts = []
        for name in result.get("keep", []):
            # Fuzzy match
            concept = name_to_concept.get(name)
            if not concept:
                # Try partial match
                for sib_name, sib_concept in name_to_concept.items():
                    if name.lower() in sib_name.lower() or sib_name.lower() in name.lower():
                        concept = sib_concept
                        break
            if concept:
                keep_concepts.append(concept)

        for name in result.get("group_into_other", []):
            concept = name_to_concept.get(name)
            if not concept:
                for sib_name, sib_concept in name_to_concept.items():
                    if name.lower() in sib_name.lower() or sib_name.lower() in name.lower():
                        concept = sib_concept
                        break
            if concept:
                other_concepts.append(concept)

        # Any siblings not in either list go to "other"
        all_concepts = {s["concept"] for s in siblings}
        assigned = set(keep_concepts) | set(other_concepts)
        for c in all_concepts - assigned:
            other_concepts.append(c)

        decisions.append({
            "parent": group["parent"],
            "parent_name": parent_name,
            "keep": keep_concepts,
            "group_into_other": other_concepts,
        })

        keep_names = [s["name"] for s in siblings if s["concept"] in keep_concepts]
        other_names = [s["name"] for s in siblings if s["concept"] in other_concepts]
        print(f"  {parent_name}: keep {len(keep_concepts)}, group {len(other_concepts)}", file=sys.stderr)

    return decisions


# ---------------------------------------------------------------------------
# Apply Grouping to Tree
# ---------------------------------------------------------------------------

def apply_grouping(tree: TreeNode, decisions: list[dict]) -> TreeNode:
    """Apply LLM grouping decisions to the tree.

    For each groupable parent, replaces grouped children with a single
    "Other" node whose values are the sum of the grouped items.
    """
    decision_map = {d["parent"]: d for d in decisions}

    def _apply(node: TreeNode) -> TreeNode:
        decision = decision_map.get(node.concept)

        if decision:
            keep_set = set(decision["keep"])
            other_set = set(decision["group_into_other"])

            new_children = []
            other_values = {}

            for child in node.children:
                if child.concept in keep_set:
                    new_children.append(_apply(child))
                elif child.concept in other_set:
                    # Accumulate into "Other"
                    for p, v in child.values.items():
                        other_values[p] = other_values.get(p, 0) + v
                else:
                    # Branch children (non-leaf) — always keep
                    new_children.append(_apply(child))

            # Add "Other" node if we grouped anything
            if other_values:
                other_node = TreeNode(f"{node.concept}__OTHER", weight=1.0)
                other_node.name = "Other"
                other_node.values = other_values
                other_node.tag = f"{node.tag}__OTHER"
                new_children.append(other_node)

            node.children = new_children
            # Recalculate is_leaf
            node.is_leaf = len(node.children) == 0
        else:
            # Recurse into children
            node.children = [_apply(c) for c in node.children]

        return node

    return _apply(tree)

def main():
    parser = argparse.ArgumentParser(description="Group XBRL siblings with LLM")
    parser.add_argument("--url", required=True, help="URL to filing HTML")
    parser.add_argument("-o", "--output", help="Output trees JSON (with grouping applied)")
    parser.add_argument("--print", dest="do_print", action="store_true")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM grouping, keep all items")
    args = parser.parse_args()

    html = fetch_url(args.url).decode('utf-8', errors='replace')
    base_url = args.url.rsplit('/', 1)[0] + '/'

    print("Building XBRL calculation trees...", file=sys.stderr)
    trees = build_statement_trees(html, base_url)
    # Note: build_statement_trees() now calls reconcile_trees() automatically
    if not trees:
        sys.exit(1)

    if not args.no_llm:
        client = Anthropic()
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if not tree:
                continue
            groups = find_groupable_siblings(tree)
            if groups:
                print(f"\n{stmt}: {len(groups)} groupable sibling sets", file=sys.stderr)
                decisions = group_siblings_with_llm(client, groups, stmt)
                trees[stmt] = apply_grouping(tree, decisions)

    if args.do_print:
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if tree:
                label = {"IS": "INCOME STATEMENT", "BS": "BALANCE SHEET (Assets)",
                         "BS_LE": "BALANCE SHEET (Liab + Equity)",
                         "CF": "CASH FLOWS"}[stmt]
                print(f"\n{'=' * 70}")
                print(label)
                print(f"{'=' * 70}")
                print_tree(tree)

    # Verify using new tree-based verify_model
    from pymodel import verify_model
    errors = verify_model(trees)
    print(f"\nPeriods: {trees.get('complete_periods', [])}", file=sys.stderr)
    if errors:
        print(f"verify_model: {len(errors)} error(s)", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
    else:
        n = len(trees.get("complete_periods", []))
        print(f"verify_model: ALL PASS ({n} periods)", file=sys.stderr)

    if args.output:
        # Serialize trees to JSON
        out = {}
        for key in ["complete_periods", "periods"]:
            if key in trees:
                out[key] = trees[key]
        out["facts"] = trees.get("facts", {})
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if tree:
                out[stmt] = tree.to_dict()
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Saved to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
