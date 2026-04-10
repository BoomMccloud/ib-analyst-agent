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
  python xbrl_group.py --url <filing_url> -o structured.json
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


# ---------------------------------------------------------------------------
# Structured Output (for pymodel)
# ---------------------------------------------------------------------------

def _filter_values(values: dict, valid_periods: set) -> dict:
    """Filter a values dict to only include valid periods."""
    return {p: v for p, v in values.items() if p in valid_periods}


def tree_to_structured(trees: dict) -> dict:
    """Convert grouped trees to the structured format pymodel expects.

    Only includes periods where ALL 3 statements have data (complete periods).
    """
    result = {}

    # Determine complete periods: intersection of IS, BS (both trees), CF root periods
    is_periods = set(trees["IS"].values.keys()) if "IS" in trees else set()
    bs_periods = set(trees["BS"].values.keys()) if "BS" in trees else set()
    bs_le_periods = set(trees["BS_LE"].values.keys()) if "BS_LE" in trees else bs_periods
    cf_periods = set(trees["CF"].values.keys()) if "CF" in trees else set()
    complete_periods = sorted(is_periods & bs_periods & bs_le_periods & cf_periods)

    if not complete_periods:
        print(f"WARNING: No complete periods found. "
              f"IS={sorted(is_periods)}, BS={sorted(bs_periods)}, CF={sorted(cf_periods)}",
              file=sys.stderr)
    else:
        print(f"Complete periods (IS+BS+CF): {complete_periods}", file=sys.stderr)
    valid = set(complete_periods)

    # --- Income Statement ---
    is_tree = trees.get("IS")
    cf_tree_ref = trees.get("CF")
    if is_tree:
        is_singles, is_categories = _extract_is_from_tree(is_tree, cf_tree_ref)
        # Filter to complete periods
        for s in is_singles:
            s["values"] = _filter_values(s["values"], valid)
        for cat in is_categories:
            cat["subtotal_values"] = _filter_values(cat["subtotal_values"], valid)
            for f in cat["flex"]:
                f["values"] = _filter_values(f["values"], valid)
            cat["other"]["values"] = _filter_values(cat["other"]["values"], valid)

        fiscal_years = {}
        for p in complete_periods:
            fiscal_years[p] = {}
            for s in is_singles:
                if p in s["values"]:
                    key = s["code"].lower()
                    fiscal_years[p][key] = s["values"][p]

        result["income_statement"] = {
            "unit": "millions",
            "fiscal_years": fiscal_years,
            "_flex_categories": is_categories,
            "_singles": is_singles,
        }

    # --- Balance Sheet ---
    bs_tree = trees.get("BS")
    bs_le_tree = trees.get("BS_LE")
    if bs_tree or bs_le_tree:
        bs_categories, bs_totals = _extract_bs_from_tree(bs_tree, bs_le_tree)
        # Filter to complete periods
        for t in bs_totals:
            t["values"] = _filter_values(t["values"], valid)
        for cat in bs_categories:
            cat["subtotal_values"] = _filter_values(cat["subtotal_values"], valid)
            for f in cat["flex"]:
                f["values"] = _filter_values(f["values"], valid)
            cat["other"]["values"] = _filter_values(cat["other"]["values"], valid)

        bs_period_dict = {}
        for t in bs_totals:
            if t["code"] == "BS_TA":
                for p in complete_periods:
                    if p in t["values"]:
                        bs_period_dict[p] = {"total_assets": t["values"][p]}

        result["balance_sheet"] = {
            "unit": "millions",
            "balance_sheet": bs_period_dict,
            "_flex_categories": bs_categories,
            "_totals": bs_totals,
        }

    # --- Cash Flows ---
    cf_tree = trees.get("CF")
    if cf_tree:
        cf_categories, cf_structural = _extract_cf_from_tree(cf_tree, trees.get("facts"))
        # Filter to complete periods
        for cat in cf_categories:
            cat["subtotal_values"] = _filter_values(cat["subtotal_values"], valid)
            for f in cat["flex"]:
                f["values"] = _filter_values(f["values"], valid)
            cat["other"]["values"] = _filter_values(cat["other"]["values"], valid)
        for s in cf_structural:
            s["values"] = _filter_values(s["values"], valid)

        result["cash_flows"] = {
            "unit": "millions",
            "_flex_categories": cf_categories,
            "_structural": cf_structural,
        }

    # --- Cash Link Fix ---
    # CF_ENDC is authoritative (from XBRL facts, includes restricted cash).
    # Override BS_CA1 (first cash item in current assets) with CF_ENDC values.
    # Adjust BS_TCA subtotal to absorb the delta.
    cf_endc = None
    for s in result.get("cash_flows", {}).get("_structural", []):
        if s["code"] == "CF_ENDC":
            cf_endc = s["values"]
            break

    if cf_endc:
        bs_cats = result.get("balance_sheet", {}).get("_flex_categories", [])
        for cat in bs_cats:
            if cat["subtotal_code"] == "BS_TCA" and cat["flex"]:
                # BS_CA1 is the first flex item (cash is always first in current assets)
                old_vals = cat["flex"][0]["values"]
                for p in complete_periods:
                    new_val = cf_endc.get(p)
                    if new_val is not None:
                        old_val = old_vals.get(p, 0)
                        delta = new_val - old_val
                        cat["flex"][0]["values"][p] = new_val
                        if abs(delta) > 0.5:
                            cat["subtotal_values"][p] = cat["subtotal_values"].get(p, 0) + delta
                cat["flex"][0]["label"] = "Cash & Equivalents (incl. restricted)"
                break

    return result


def _extract_is_from_tree(tree: TreeNode, cf_tree: TreeNode = None) -> tuple[list, list]:
    """Extract IS singles + categories using position in tree, not names.

    Position-based rules:
    - Root: bottom line (NI available to common, or NI)
    - INC_NET: matched to CF operating section's first child value
    - Walk down from root: each level is a structural relationship
    - Nodes with 3+ children that were grouped: become categories
    - All others: singles
    """
    singles = []
    categories = []

    # Find INC_NET by cross-referencing IS and CF trees (Option 4).
    #
    # The CF tree contains a NI leaf (ProfitLoss or NetIncomeLoss).
    # The IS tree may use a narrower concept (e.g., ContinuingOperations).
    # To reconcile: find the IS node whose value matches CF's NI value.
    # Walk upward from the IS bottom to find the matching ancestor.
    cf_ni_values = None
    if cf_tree:
        def _find_ni_in_cf(node):
            """Search CF subtree for the NI leaf."""
            concept = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept
            if concept in ("ProfitLoss", "NetIncomeLoss") and node.values:
                return node.values
            for child in node.children:
                result = _find_ni_in_cf(child)
                if result:
                    return result
            return None
        cf_ni_values = _find_ni_in_cf(cf_tree)

    # cf_ni_values is authoritative — the CF statement is the bridge.
    # Even if the IS tree uses a narrower concept (e.g., ContinuingOperations),
    # we use CF's NI for INC_NET to ensure the cross-statement link holds.

    # Walk the IS tree by position
    # The tree is a cascade: each node = parent - children (with weights)
    idx = [0]  # mutable counter for unique codes

    def _assign_code(node: TreeNode, depth: int, parent_code: str = None):
        """Assign model codes by position in the IS tree."""
        # Determine this node's role by its position
        code = None
        label = node.name

        if depth == 0:
            # Root — bottom line of IS
            # Don't assign a code; walk children
            pass
        elif depth == 1:
            # Direct children of root
            if node.weight > 0:
                # The positive child at depth 1 is Net Income
                # Cross-reference with CF to get the right value
                code = "INC_NET"
                label = "Net Income"
                if cf_ni_values:
                    node = TreeNode(node.concept, node.weight)
                    node.values = cf_ni_values
                    node.name = "Net Income"
                    node.children = []
                    node.is_leaf = True
            # Negative children (preferred dividends, NCI) — skip for model
        elif node.weight < 0 and not node.children:
            # Negative leaf = subtracted item (Tax, COGS, expenses)
            # Use a generic code
            idx[0] += 1
            code = f"IS_{idx[0]}"
            label = node.name
        elif node.weight > 0 and not node.children:
            # Positive leaf
            idx[0] += 1
            code = f"IS_{idx[0]}"
            label = node.name

        # For nodes with children: they're structural (cascade)
        # Each child is a component — walk them recursively
        if node.children:
            # This is a subtotal node
            # Assign it a code based on what it represents structurally
            if depth >= 2 and node.values:
                idx[0] += 1
                code = f"IS_{idx[0]}"
                label = node.name

                # If it has leaf children, make it a category
                if node.has_groupable_children or any(c.is_leaf for c in node.children):
                    flex_prefix = f"IS{idx[0]}_"
                    flex_items = []
                    catch_all_values = {}
                    for i, child in enumerate(node.children):
                        if "__OTHER" in child.concept:
                            catch_all_values = child.values
                        elif child.is_leaf and child.values:
                            flex_items.append({
                                "code": f"{flex_prefix}{i+1}",
                                "label": child.name,
                                "values": child.values,
                            })
                        elif not child.is_leaf:
                            # Branch child — add as single and recurse
                            _assign_code(child, depth + 1, code)

                    if flex_items:
                        categories.append({
                            "subtotal_code": code,
                            "subtotal_label": label,
                            "subtotal_values": node.values,
                            "flex": flex_items,
                            "other": {"code": f"{flex_prefix}OTH", "label": "Other",
                                      "values": catch_all_values},
                        })
                        return  # Don't add as single too
                    # else fall through to add as single

            if not code:
                for child in node.children:
                    _assign_code(child, depth + 1, code)
                return

        if code and node.values:
            singles.append({"code": code, "label": label, "values": node.values})

    # Walk depth-first from root
    # But first, let's use a simpler approach: flatten the cascade
    # The IS tree is a chain of subtractions. Extract each node as a single.
    _flatten_is_cascade(tree, singles, categories, cf_ni_values)

    return singles, categories


def _flatten_is_cascade(node: TreeNode, singles: list, categories: list,
                         cf_ni_values: dict = None, depth: int = 0):
    """Flatten the IS cascade tree into singles and categories by position.

    Rules:
    - Each node with children is a subtotal (structural)
    - Leaf nodes are line items
    - Nodes with grouped children become categories
    - INC_NET is cross-referenced with CF's Net Income
    """
    # Position-based code assignment
    # depth 0 = root (bottom line, may be NI-avail-to-common or NI)
    # We walk the tree and emit every node that has values

    # First pass: collect all nodes in tree order with their structural role
    items = []
    _collect_is_nodes(node, items, depth=0)

    # Assign INC_NET: use CF NI values directly (authoritative)
    ni_assigned = False
    if cf_ni_values:
        singles.append({"code": "INC_NET", "label": "Net Income",
                        "values": dict(cf_ni_values)})
        ni_assigned = True

    for item in items:

        node = item["node"]
        if not node.values:
            continue

        # Nodes with grouped leaf children → category
        if node.children and any(c.is_leaf for c in node.children):
            flex_items = []
            catch_all_values = {}
            code = f"IS_{item['index']}"
            for i, child in enumerate(node.children):
                if "__OTHER" in child.concept:
                    catch_all_values = child.values
                elif child.is_leaf and child.values:
                    flex_items.append({
                        "code": f"IS{item['index']}_{i+1}",
                        "label": child.name,
                        "values": child.values,
                    })
            if flex_items:
                categories.append({
                    "subtotal_code": code,
                    "subtotal_label": node.name,
                    "subtotal_values": node.values,
                    "flex": flex_items,
                    "other": {"code": f"IS{item['index']}_OTH", "label": "Other",
                              "values": catch_all_values},
                })
                continue

        # Leaf nodes and subtotals without leaf children → singles
        if node.is_leaf or (node.children and not any(c.is_leaf for c in node.children)):
            code = f"IS_{item['index']}"
            singles.append({"code": code, "label": node.name, "values": node.values})

    # If INC_NET wasn't found via CF match, use root
    if not ni_assigned and node.values:
        singles.insert(0, {"code": "INC_NET", "label": "Net Income",
                           "values": items[0]["node"].values if items else {}})


def _collect_is_nodes(node: TreeNode, items: list, depth: int):
    """Collect all meaningful nodes from IS tree in display order."""
    items.append({"node": node, "depth": depth, "index": len(items)})
    for child in node.children:
        _collect_is_nodes(child, items, depth + 1)


def _extract_bs_from_tree(assets_tree: TreeNode | None,
                           liab_eq_tree: TreeNode | None) -> tuple[list, list]:
    """Extract BS categories + totals using position in tree, not names.

    Position-based rules:
    - Assets tree root = BS_TA (Total Assets)
    - Root's child with sub-children = BS_TCA (Current Assets)
    - Root's remaining children = Non-Current Assets
    - L&E tree root's first child = BS_TL (Liabilities) — largest child
    - L&E tree root's last non-zero child = BS_TE (Equity)
    - Under TL: child with sub-children = BS_TCL
    - Under TL: remaining children = Non-Current Liabilities
    """
    categories = []
    totals = []

    def _make_category(code, label, flex_prefix, catch_all_code, subtotal_values, children):
        flex_items = []
        catch_all_values = {}
        for i, child in enumerate(children):
            if "__OTHER" in child.concept:
                catch_all_values = child.values
            elif child.values:
                child_code = f"{flex_prefix}{i+1}"
                flex_items.append({
                    "code": child_code,
                    "label": child.name,
                    "values": child.values,
                })
        categories.append({
            "subtotal_code": code,
            "subtotal_label": label,
            "subtotal_values": subtotal_values,
            "flex": flex_items,
            "other": {"code": catch_all_code, "label": "Other", "values": catch_all_values},
        })

    # --- Assets tree ---
    if assets_tree and assets_tree.values:
        totals.append({"code": "BS_TA", "label": "Total Assets", "values": assets_tree.values})

        # Find current assets: the child that itself has sub-children (deepest branch)
        tca_node = None
        for child in assets_tree.children:
            if child.children and not child.is_leaf:
                tca_node = child
                break

        if tca_node:
            _make_category("BS_TCA", "Total Current Assets", "BS_CA", "BS_CA_OTH",
                           tca_node.values, tca_node.children)

        # NCA = all other children of Assets (not current assets)
        nca_children = [c for c in assets_tree.children if c != tca_node]
        nca_values = {}
        for child in nca_children:
            for p, v in child.values.items():
                nca_values[p] = nca_values.get(p, 0) + v
        if nca_values:
            _make_category("BS_TNCA", "Total Non-Current Assets", "BS_NCA", "BS_NCA_OTH",
                           nca_values, nca_children)

    # --- Liabilities + Equity tree ---
    if liab_eq_tree and liab_eq_tree.children:
        # Position-based: Liabilities and Equity are the two main branch children
        # In the filing, Liabilities always comes first, Equity second.
        # Skip children with no values (e.g., "Commitments and Contingencies" = 0)
        branch_children = [c for c in liab_eq_tree.children
                           if c.values and any(v != 0 for v in c.values.values())]

        if len(branch_children) >= 2:
            # Equity is always the LAST branch child
            # Everything before it is Liabilities (or components thereof)
            equity_node = branch_children[-1]
            liab_children = branch_children[:-1]

            # If there's a single Liabilities wrapper, use it directly
            # If there are multiple liabilities components (KO), synthesize TL
            if len(liab_children) == 1:
                liab_node = liab_children[0]
            else:
                # No Liabilities wrapper — synthesize from components
                liab_node = TreeNode("__LIABILITIES_SYNTHETIC", weight=1.0)
                liab_node.name = "Liabilities"
                liab_values = {}
                for child in liab_children:
                    for p, v in child.values.items():
                        liab_values[p] = liab_values.get(p, 0) + v
                    liab_node.add_child(child)
                liab_node.values = liab_values

            # Liabilities
            if liab_node.values:
                totals.append({"code": "BS_TL", "label": "Total Liabilities", "values": liab_node.values})

                # Find current liabilities: child with sub-children
                tcl_node = None
                for child in liab_node.children:
                    if child.children and not child.is_leaf:
                        tcl_node = child
                        break

                if tcl_node:
                    _make_category("BS_TCL", "Total Current Liabilities", "BS_CL", "BS_CL_OTH",
                                   tcl_node.values, tcl_node.children)

                # NCL = remaining children
                ncl_children = [c for c in liab_node.children if c != tcl_node]
                ncl_values = {}
                for child in ncl_children:
                    for p, v in child.values.items():
                        ncl_values[p] = ncl_values.get(p, 0) + v
                if ncl_values:
                    _make_category("BS_TNCL", "Total Non-Current Liabilities", "BS_NCL", "BS_NCL_OTH",
                                   ncl_values, ncl_children)

            # Equity
            if equity_node and equity_node.values:
                # BS_TE goes in categories only (not totals) to avoid double-counting
                # in load_filing which merges both _totals and _flex_categories
                _make_category("BS_TE", "Total Equity", "BS_EQ", "BS_EQ_OTH",
                               equity_node.values, equity_node.children)
        elif len(branch_children) == 1:
            # Single child — likely Liabilities only, Equity separate
            totals.append({"code": "BS_TL", "label": "Total Liabilities",
                           "values": branch_children[0].values})

    return categories, totals


def _extract_cf_from_tree(tree: TreeNode, facts: dict = None) -> tuple[list, list]:
    """Walk CF tree and extract categories + structural items.

    TODO: Phase 1c - Deterministic Cross-Statement Reconciliation
    For D&A and SBC links, prefer standard US-GAAP XBRL tags (us-gaap:DepreciationDepletionAndAmortization, 
    us-gaap:ShareBasedCompensation) over value-matching heuristics. Only use value-matching as a tie-breaker 
    if exact tags are missing.

    CF_ENDC (ending cash) comes from the XBRL facts dict, not the tree.
    The tree root is the net change; ending cash is an instant-context fact.
    """
    categories = []
    structural = []

    CF_MAP = {
        "NetCashProvidedByUsedInOperatingActivities": ("CF_OPCF", "Operating Cash Flow", "CF_OP", "CF_OP_OTH"),
        "NetCashProvidedByUsedInInvestingActivities": ("CF_INVCF", "Investing Cash Flow", "CF_INV", "CF_INV_OTH"),
        "NetCashProvidedByUsedInFinancingActivities": ("CF_FINCF", "Financing Cash Flow", "CF_FIN", "CF_FIN_OTH"),
    }

    # CF_NETCH = tree root (the net change in cash)
    if tree.values:
        structural.append({"code": "CF_NETCH", "label": "Net Change in Cash",
                           "values": tree.values})

    # CF_ENDC = from facts dict (instant context, not in the tree)
    if facts:
        endc_tags = [
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        ]
        for tag in endc_tags:
            if tag in facts:
                structural.append({"code": "CF_ENDC", "label": "Ending Cash",
                                   "values": facts[tag]})
                break

    # CF_BEGC from prior-period CF_ENDC
    endc = next((s for s in structural if s["code"] == "CF_ENDC"), None)
    if endc:
        periods = sorted(endc["values"].keys())
        begc = {}
        for i, p in enumerate(periods):
            if i > 0:
                begc[p] = endc["values"][periods[i - 1]]
        if begc:
            structural.append({"code": "CF_BEGC", "label": "Beginning Cash",
                               "values": begc})

    seen_codes = set()

    def _walk(node: TreeNode):
        concept_name = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept

        # Check for section subtotals
        for pattern, (code, label, flex_prefix, catch_all_code) in CF_MAP.items():
            if concept_name.startswith(pattern) and code not in seen_codes and node.values:
                seen_codes.add(code)

                # Find the child with the most leaf items (the actual line items).
                # GE has OpCF -> ContinuingOps (19 leaves) + DiscontinuedOps (1 leaf).
                # We want the 19-leaf node, not ContinuingOps as a single flex item.
                items_node = node
                # Drill: pick the child with the most children at each level
                while True:
                    branch_kids = [c for c in items_node.children if c.children]
                    if not branch_kids:
                        break
                    # Pick the branch child with the most leaves
                    best = max(branch_kids, key=lambda c: len(c.children))
                    if len(best.children) > len(items_node.children):
                        items_node = best
                    else:
                        break
                if not items_node.children:
                    items_node = node

                flex_items = []
                catch_all_values = {}
                for i, child in enumerate(items_node.children):
                    if "__OTHER" in child.concept:
                        catch_all_values = child.values
                    elif child.values:
                        child_code = f"{flex_prefix}{i+1}"
                        flex_items.append({
                            "code": child_code,
                            "label": child.name,
                            "values": child.values,
                        })

                categories.append({
                    "subtotal_code": code,
                    "subtotal_label": label,
                    "subtotal_values": node.values,
                    "flex": flex_items,
                    "other": {"code": catch_all_code, "label": "Other", "values": catch_all_values},
                })
                return  # Don't recurse into children we already processed

        for child in node.children:
            _walk(child)

    _walk(tree)

    return categories, structural


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Group XBRL siblings with LLM")
    parser.add_argument("--url", required=True, help="URL to filing HTML")
    parser.add_argument("-o", "--output", help="Output structured JSON")
    parser.add_argument("--print", dest="do_print", action="store_true")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM grouping, keep all items")
    args = parser.parse_args()

    html = fetch_url(args.url).decode('utf-8', errors='replace')
    base_url = args.url.rsplit('/', 1)[0] + '/'

    print("Building XBRL calculation trees...", file=sys.stderr)
    trees = build_statement_trees(html, base_url)
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

    # Build structured output
    structured = tree_to_structured(trees)

    # Verify through pymodel
    sys.path.insert(0, '.')
    from pymodel import load_filing, verify_model
    filing = load_filing(structured)
    errors = verify_model(filing)
    print(f"\nPeriods: {filing['periods']}", file=sys.stderr)
    if errors:
        print(f"verify_model: {len(errors)} error(s)", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
    else:
        print(f"verify_model: ALL PASS ({len(filing['periods'])} periods)", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(structured, f, indent=2)
        print(f"Saved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
