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
import re
import sys
from collections import defaultdict

from parse_xbrl_facts import build_xbrl_facts_dict
from sec_utils import fetch_url


# ---------------------------------------------------------------------------
# Calculation Linkbase Parser
# ---------------------------------------------------------------------------

def fetch_cal_linkbase(html: str, base_url: str) -> str | None:
    """Find and fetch the calculation linkbase referenced by the filing.

    Looks for the schema reference in the HTML, then finds _cal.xml
    in the schema's linkbase references.
    """
    # Find the schema reference
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None

    schema_href = m.group(1)
    # Schema is relative to the filing
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href

    # Fetch schema to find _cal.xml
    schema = fetch_url(schema_href).decode('utf-8', errors='replace')

    # Find calculation linkbase reference
    cal_pat = re.compile(r'href="([^"]*_cal\.xml)"', re.IGNORECASE)
    cal_m = cal_pat.search(schema)
    if not cal_m:
        return None

    cal_href = cal_m.group(1)
    if not cal_href.startswith('http'):
        cal_href = base_url + cal_href

    return fetch_url(cal_href).decode('utf-8', errors='replace')


def parse_calc_linkbase(cal_xml: str) -> dict:
    """Parse calculation linkbase into per-section trees.

    Returns: {role_name: {parent_concept: [(child_concept, weight), ...]}}
    """
    loc_pat = re.compile(
        r'<link:loc[^>]*xlink:label="([^"]+)"[^>]*xlink:href="[^#]*#([^"]+)"',
        re.IGNORECASE
    )
    arc_pat = re.compile(
        r'<link:calculationArc[^>]*?'
        r'xlink:from="([^"]+)"[^>]*?'
        r'xlink:to="([^"]+)"[^>]*?'
        r'weight="([^"]+)"',
        re.IGNORECASE | re.DOTALL
    )
    arc_pat2 = re.compile(
        r'<link:calculationArc[^>]*?'
        r'weight="([^"]+)"[^>]*?'
        r'xlink:from="([^"]+)"[^>]*?'
        r'xlink:to="([^"]+)"',
        re.IGNORECASE | re.DOTALL
    )
    section_pat = re.compile(
        r'<link:calculationLink[^>]*xlink:role="([^"]+)"[^>]*>'
        r'([\s\S]*?)</link:calculationLink>',
        re.IGNORECASE
    )

    results = {}
    for section_m in section_pat.finditer(cal_xml):
        role = section_m.group(1).split('/')[-1]
        body = section_m.group(2)

        # Parse locators within this section
        sec_locs = {}
        for m in loc_pat.finditer(body):
            sec_locs[m.group(1)] = m.group(2)

        # Parse arcs
        children = defaultdict(list)
        seen = set()
        for m in arc_pat.finditer(body):
            parent = sec_locs.get(m.group(1), m.group(1))
            child = sec_locs.get(m.group(2), m.group(2))
            weight = float(m.group(3))
            key = (parent, child)
            if key not in seen:
                children[parent].append((child, weight))
                seen.add(key)

        for m in arc_pat2.finditer(body):
            parent = sec_locs.get(m.group(2), m.group(2))
            child = sec_locs.get(m.group(3), m.group(3))
            weight = float(m.group(1))
            key = (parent, child)
            if key not in seen:
                children[parent].append((child, weight))
                seen.add(key)

        if children:
            results[role] = dict(children)

    return results


# ---------------------------------------------------------------------------
# Statement Detection
# ---------------------------------------------------------------------------

# Map role names to statement types
STATEMENT_ROLE_PATTERNS = {
    "IS": [r"consolidatedstatements?of(?:net)?(?:income|operations|earnings)",
           r"statements?of(?:consolidated)?(?:net)?(?:income|operations|earnings)",
           r"incomestatements?"],
    "BS": [r"consolidatedbalancesheets?",
           r"statements?of(?:consolidated)?financialposition",
           r"balancesheets?"],
    "CF": [r"consolidatedstatements?ofcashflows?",
           r"statements?of(?:consolidated)?cashflows?",
           r"cashflows?statements?"],
}


def classify_roles(roles: list[str]) -> dict:
    """Map role names to IS/BS/CF. Returns {statement: role_name}."""
    result = {}
    for role in roles:
        role_lower = role.lower().replace("_", "").replace("-", "")
        # Skip alternative calculations
        if "alternative" in role_lower:
            continue
        for stmt, patterns in STATEMENT_ROLE_PATTERNS.items():
            if stmt in result:
                continue
            for pat in patterns:
                if re.search(pat, role_lower):
                    result[stmt] = role
                    break
    return result


# ---------------------------------------------------------------------------
# Tree Building
# ---------------------------------------------------------------------------

def _concept_to_tag(concept: str) -> str:
    """Convert XBRL concept ID to tag format used in facts dict.

    e.g., 'us-gaap_Assets' -> 'us-gaap:Assets'
         'ge_ContractAssets' -> 'ge:ContractAssets'
    """
    # Concepts use underscore separator, tags use colon
    parts = concept.split('_', 1)
    if len(parts) == 2:
        return f"{parts[0]}:{parts[1]}"
    return concept


def _clean_name(concept: str) -> str:
    """Convert concept ID to readable name.

    e.g., 'us-gaap_CashAndCashEquivalentsAtCarryingValue' -> 'Cash And Cash Equivalents'
    """
    name = concept.split('_', 1)[-1]  # Remove namespace prefix
    # Insert spaces before capitals
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    # Remove common suffixes
    for suffix in ['AtCarryingValue', 'Net Current', 'Noncurrent']:
        name = name.replace(suffix, '')
    return name.strip()


class TreeNode:
    """A node in the XBRL calculation tree."""

    def __init__(self, concept: str, weight: float = 1.0):
        self.concept = concept
        self.tag = _concept_to_tag(concept)
        self.name = _clean_name(concept)
        self.weight = weight  # +1 or -1 relative to parent
        self.children: list[TreeNode] = []
        self.values: dict[str, float] = {}  # {period: value}
        self.is_leaf = True
        self.role: str | None = None    # e.g., "BS_TA", "INC_NET", "CF_ENDC"

    def add_child(self, child: 'TreeNode'):
        self.children.append(child)
        self.is_leaf = False

    @property
    def has_groupable_children(self) -> bool:
        """True if this node has 3+ leaf children (candidates for grouping)."""
        leaf_children = [c for c in self.children if c.is_leaf]
        return len(leaf_children) >= 3

    @property
    def leaf_children(self) -> list['TreeNode']:
        return [c for c in self.children if c.is_leaf]

    @property
    def branch_children(self) -> list['TreeNode']:
        return [c for c in self.children if not c.is_leaf]

    def to_dict(self) -> dict:
        d = {
            "concept": self.concept,
            "tag": self.tag,
            "name": self.name,
            "weight": self.weight,
            "values": self.values,
            "is_leaf": self.is_leaf,
        }
        if self.role:
            d["role"] = self.role
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TreeNode":
        """Reconstruct a TreeNode from its to_dict() output."""
        node = cls(d["concept"], d.get("weight", 1.0))
        node.name = d.get("name", node.name)
        node.tag = d.get("tag", node.tag)
        node.values = d.get("values", {})
        node.role = d.get("role")
        node.is_leaf = d.get("is_leaf", True)
        for child_dict in d.get("children", []):
            node.add_child(cls.from_dict(child_dict))
        return node


def build_tree(calc_children: dict, facts: dict, root_concept: str) -> TreeNode:
    """Build a tree from the calculation linkbase, attaching values.

    Args:
        calc_children: {parent_concept: [(child_concept, weight), ...]}
        facts: {xbrl_tag: {period: value}} from iXBRL
        root_concept: the root concept to start from
    """
    visited = set()

    def _build(concept, weight=1.0):
        if concept in visited:
            return None
        visited.add(concept)

        node = TreeNode(concept, weight)

        # Attach values from facts
        tag = _concept_to_tag(concept)
        if tag in facts:
            node.values = dict(facts[tag])

        # Recurse into children
        kids = calc_children.get(concept, [])
        for child_concept, child_weight in kids:
            child_node = _build(child_concept, child_weight)
            if child_node:
                node.add_child(child_node)

        return node

    return _build(root_concept)


def find_roots(calc_children: dict) -> list[str]:
    """Find root concepts (parents that aren't children of anything)."""
    all_parents = set(calc_children.keys())
    all_children = set(c for kids in calc_children.values() for c, _ in kids)
    return sorted(all_parents - all_children)


# ---------------------------------------------------------------------------
# Sibling Group Identification
# ---------------------------------------------------------------------------

def find_groupable_siblings(tree: TreeNode) -> list[dict]:
    """Walk the tree and find all sibling sets eligible for LLM grouping.

    A sibling set is groupable when:
    - Parent has 3+ leaf children
    - All siblings are additive (same sign) under the parent

    Returns list of:
      {"parent": concept, "parent_name": str,
       "siblings": [{"concept", "name", "tag", "values", "weight"}],
       "parent_total": {period: value}}
    """
    groups = []

    def _walk(node: TreeNode):
        leaf_kids = node.leaf_children
        if len(leaf_kids) >= 3:
            siblings = []
            for child in leaf_kids:
                siblings.append({
                    "concept": child.concept,
                    "name": child.name,
                    "tag": child.tag,
                    "weight": child.weight,
                    "values": child.values,
                })
            # Sort by average absolute value (descending)
            def avg_abs(s):
                vals = s["values"]
                if not vals:
                    return 0
                return sum(abs(v) for v in vals.values()) / len(vals)
            siblings.sort(key=avg_abs, reverse=True)

            groups.append({
                "parent": node.concept,
                "parent_name": node.name,
                "parent_total": node.values,
                "siblings": siblings,
            })

        # Recurse into branch children
        for child in node.branch_children:
            _walk(child)

    _walk(tree)
    return groups


# ---------------------------------------------------------------------------
# Pretty Printing
# ---------------------------------------------------------------------------

def print_tree(node: TreeNode, indent: int = 0, periods: list[str] = None):
    """Print the tree with values."""
    if periods is None:
        # Collect all periods from the tree
        all_periods = set()
        def _collect(n):
            all_periods.update(n.values.keys())
            for c in n.children:
                _collect(c)
        _collect(node)
        periods = sorted(all_periods)[-3:]  # Last 3 periods

    sign = {1.0: "+", -1.0: "-"}.get(node.weight, "?")
    prefix = f"{'  ' * indent}{sign} " if indent > 0 else ""
    name = node.name[:40]

    # Values
    val_strs = []
    for p in periods:
        v = node.values.get(p)
        if v is not None:
            val_strs.append(f"{v:>12,.0f}")
        else:
            val_strs.append(f"{'—':>12s}")
    vals = "  ".join(val_strs)

    groupable = " [GROUPABLE]" if node.has_groupable_children else ""
    print(f"{prefix}{name:40s}  {vals}{groupable}")

    for child in node.children:
        print_tree(child, indent + 1, periods)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reconciliation and Invariants
# ---------------------------------------------------------------------------

def find_node_by_role(tree: TreeNode, role: str) -> TreeNode | None:
    """Recursively search the tree for a node with the given role."""
    if getattr(tree, "role", None) == role:
        return tree
    for child in tree.children:
        result = find_node_by_role(child, role)
        if result:
            return result
    return None

def _tag_bs_positions(assets_tree: TreeNode | None, liab_eq_tree: TreeNode | None):
    """Tag BS nodes by position in the tree."""
    if assets_tree and assets_tree.values:
        # BS_TA = root of Assets tree
        assets_tree.role = "BS_TA"

        # BS_TCA = first child that has its own children (not a leaf)
        found_tca = False
        for child in assets_tree.children:
            if child.children and not child.is_leaf:
                child.role = "BS_TCA"
                # Tag first flex item as BS_CASH (for cash link override later)
                if child.children:
                    child.children[0].role = "BS_CASH"
                found_tca = True
                break
        if not found_tca:
            print("WARNING: Could not identify BS_TCA (Current Assets) in Assets tree",
                  file=sys.stderr)
    elif assets_tree:
        print("WARNING: Assets tree has no values — skipping BS tagging", file=sys.stderr)

    if liab_eq_tree and liab_eq_tree.children:
        liab_eq_tree.role = "BS_TLE"
        # Filter out zero-valued children (e.g., "Commitments and Contingencies")
        branch_children = [
            c for c in liab_eq_tree.children
            if c.values and any(v != 0 for v in c.values.values())
        ]

        if len(branch_children) >= 2:
            # Equity is ALWAYS the LAST non-zero branch child
            equity_node = branch_children[-1]
            equity_node.role = "BS_TE"

            # Everything before equity = liabilities
            liab_children = branch_children[:-1]

            if len(liab_children) == 1:
                # Single Liabilities wrapper — use it directly
                liab_node = liab_children[0]
            else:
                # Multiple liabilities components (KO pattern) — synthesize wrapper
                liab_node = TreeNode("__LIABILITIES_SYNTHETIC", weight=1.0)
                liab_node.name = "Liabilities"
                liab_values = {}
                for child in liab_children:
                    for p, v in child.values.items():
                        liab_values[p] = liab_values.get(p, 0) + v
                    liab_node.add_child(child)
                liab_node.values = liab_values
                # Replace L&E tree children so the synthetic node is in the tree
                liab_eq_tree.children = [liab_node, equity_node]

            liab_node.role = "BS_TL"

            # BS_TCL = first child of liabilities that has sub-children
            for child in liab_node.children:
                if child.children and not child.is_leaf:
                    child.role = "BS_TCL"
                    break

        elif len(branch_children) == 1:
            # Single child — liabilities only, equity may be missing
            branch_children[0].role = "BS_TL"
            print("WARNING: Could not identify BS_TE (Equity) in L&E tree — "
                  "only 1 non-zero branch child found", file=sys.stderr)
        else:
            print("WARNING: Could not identify BS_TL/BS_TE in L&E tree — "
                  "no non-zero branch children found", file=sys.stderr)

def _find_leaf_by_keywords(tree: TreeNode, keywords: list[str]) -> TreeNode | None:
    """Find a leaf node whose name contains all keywords (case-insensitive).
    
    Example: _find_leaf_by_keywords(is_tree, ["depreciation"]) finds
    "Depreciation And Amortization" but not "Accumulated Depreciation" (which is on BS).
    """
    name_lower = tree.name.lower()
    if tree.is_leaf and all(kw in name_lower for kw in keywords):
        return tree
    for child in tree.children:
        result = _find_leaf_by_keywords(child, keywords)
        if result:
            return result
    return None


def _find_leaf_by_timeseries(tree: TreeNode, periods: list[str],
                              target_values: dict[str, float]) -> TreeNode | None:
    """Find a leaf node whose values match target across ALL periods (within 0.5).
    
    Why ALL periods? A single-period match risks collisions — e.g., D&A = $150M in 2024,
    but "Changes in Inventory" also happens to be $150M in 2024. Matching across ALL
    5 years eliminates this: a coincidental collision across 5 periods is near impossible.
    
    Example: If IS D&A has values {2020: 100, 2021: 110, 2022: 120, 2023: 130, 2024: 150},
    this finds the CF node with the same 5-year pattern.
    """
    if tree.is_leaf and tree.values:
        matched = 0
        total = 0
        for p in periods:
            target = target_values.get(p, 0)
            actual = tree.values.get(p, 0)
            if target != 0:
                total += 1
                if abs(actual - target) < 0.5:
                    matched += 1
        # ALL non-zero periods must match
        if total > 0 and matched == total:
            return tree
    for child in tree.children:
        result = _find_leaf_by_timeseries(child, periods, target_values)
        if result:
            return result
    return None


def _tag_da_sbc_nodes(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    """Tag D&A and SBC leaf nodes in IS and CF trees.
    
    Strategy:
    1. Find IS leaf by keyword (e.g., "depreciation" for D&A)
    2. Find matching CF leaf by full time-series value match (collision-safe)
    3. Tag both with roles (IS_DA/CF_DA, IS_SBC/CF_SBC)
    """
    if not is_tree or not cf_tree:
        return
    
    # We only search inside CF's Operating Cash Flow section for D&A and SBC,
    # because that's where they appear in the indirect method CF statement.
    cf_opcf = find_node_by_role(cf_tree, "CF_OPCF")
    if not cf_opcf:
        return
    
    # Get periods from IS tree (only non-zero periods)
    periods = [p for p in (is_tree.values.keys() if is_tree.values else [])
               if is_tree.values.get(p, 0) != 0]
    if not periods:
        return
    
    # --- D&A ---
    # Try "depreciation" first, fall back to "amortization"
    is_da = _find_leaf_by_keywords(is_tree, ["depreciation"])
    if not is_da:
        is_da = _find_leaf_by_keywords(is_tree, ["amortization"])
    
    if is_da:
        is_da.role = "IS_DA"
        # Find the matching CF node by full time-series match
        cf_da = _find_leaf_by_timeseries(cf_opcf, periods, is_da.values)
        if cf_da:
            cf_da.role = "CF_DA"
        else:
            print("WARNING: Could not find CF D&A node matching IS D&A values",
                  file=sys.stderr)
    
    # --- SBC ---
    # Try "stock" + "compensation" first, fall back to "share" + "compensation"
    is_sbc = _find_leaf_by_keywords(is_tree, ["stock", "compensation"])
    if not is_sbc:
        is_sbc = _find_leaf_by_keywords(is_tree, ["share", "compensation"])
    
    if is_sbc:
        is_sbc.role = "IS_SBC"
        cf_sbc = _find_leaf_by_timeseries(cf_opcf, periods, is_sbc.values)
        if cf_sbc:
            cf_sbc.role = "CF_SBC"
        else:
            print("WARNING: Could not find CF SBC node matching IS SBC values",
                  file=sys.stderr)

def _tag_cf_positions(cf_tree: TreeNode | None, facts: dict) -> dict | None:
    """Tag CF nodes by position. Returns CF_ENDC values dict (from facts, not tree)."""
    cf_endc_values = None

    # Look up CF_ENDC from XBRL facts (instant context, not in any tree)
    if facts:
        endc_tags = [
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        ]
        for tag in endc_tags:
            if tag in facts:
                cf_endc_values = facts[tag]
                break

    if not cf_tree:
        return cf_endc_values

    # Tag the root as net change in cash
    cf_tree.role = "CF_NETCH"

    # Map concept name patterns to roles
    CF_ROLE_MAP = {
        "NetCashProvidedByUsedInOperatingActivities": "CF_OPCF",
        "NetCashProvidedByUsedInInvestingActivities": "CF_INVCF",
        "NetCashProvidedByUsedInFinancingActivities": "CF_FINCF",
    }

    seen_roles = set()

    def _walk_and_tag(node: TreeNode):
        """Walk the CF tree depth-first. Tag section nodes by concept pattern."""
        concept_name = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept

        for pattern, role in CF_ROLE_MAP.items():
            if concept_name.startswith(pattern) and role not in seen_roles and node.values:
                node.role = role
                seen_roles.add(role)
                # Don't recurse into children — we already found the section node
                return

        # Also tag the NI leaf inside CF (ProfitLoss or NetIncomeLoss)
        if concept_name in ("ProfitLoss", "NetIncomeLoss") and node.values and not node.children:
            node.role = "INC_NET_CF"

        for child in node.children:
            _walk_and_tag(child)

    _walk_and_tag(cf_tree)

    # --- Tag FX impact node (if present) ---
    # Multinational companies have an "Effect of Exchange Rate" node as a sibling
    # of OPCF/INVCF/FINCF under the CF root. If omitted, the cash proof will show
    # non-zero errors for companies like AAPL, AMZN, GE.
    FX_PATTERNS = ["EffectOfExchangeRate", "EffectOfForeignExchangeRate"]
    for child in cf_tree.children:
        concept_name = child.concept.split('_', 1)[-1] if '_' in child.concept else child.concept
        for pat in FX_PATTERNS:
            if concept_name.startswith(pat) and child.values:
                child.role = "CF_FX"
                break

    # NI is typically inside OPCF (which we skip recursing into above).
    # Search OPCF's children separately for the NI leaf.
    opcf_node = find_node_by_role(cf_tree, "CF_OPCF")
    if opcf_node and not find_node_by_role(cf_tree, "INC_NET_CF"):
        def _find_ni_in_subtree(node: TreeNode):
            cn = node.concept.split('_', 1)[-1] if '_' in node.concept else node.concept
            if cn in ("ProfitLoss", "NetIncomeLoss") and node.values and not node.children:
                node.role = "INC_NET_CF"
                return True
            for child in node.children:
                if _find_ni_in_subtree(child):
                    return True
            return False
        _find_ni_in_subtree(opcf_node)

    expected_roles = {"CF_OPCF", "CF_INVCF", "CF_FINCF"}
    missing = expected_roles - seen_roles
    if missing:
        print(f"WARNING: Could not identify CF roles: {sorted(missing)}", file=sys.stderr)

    # Check if INC_NET_CF was found
    if not find_node_by_role(cf_tree, "INC_NET_CF"):
        print("WARNING: Could not identify INC_NET_CF (Net Income) in CF tree",
              file=sys.stderr)

    return cf_endc_values

def _tag_is_positions(is_tree: TreeNode | None, cf_tree: TreeNode | None):
    """Tag IS Net Income node using CF's NI as authoritative source.

    Strategy: value-match IS depth-1 children against CF's authoritative NI.
    Falls back to first positive-weight child only if no value match found.
    """
    if not is_tree:
        return

    # Tag IS_REVENUE and IS_COGS
    def _find_by_kw(node, kw):
        if kw in node.name.lower(): return node
        for c in node.children:
            res = _find_by_kw(c, kw)
            if res: return res
        return None

    rev_node = _find_by_kw(is_tree, "revenue") or _find_by_kw(is_tree, "sales")
    if rev_node: rev_node.role = "IS_REVENUE"
    cogs_node = _find_by_kw(is_tree, "cost of revenue") or _find_by_kw(is_tree, "cost of sales") or _find_by_kw(is_tree, "cost of goods")
    if cogs_node: cogs_node.role = "IS_COGS"

    # Find CF's NI values (the authoritative source)
    cf_ni_values = None
    if cf_tree:
        cf_ni_node = find_node_by_role(cf_tree, "INC_NET_CF")
        if cf_ni_node:
            cf_ni_values = cf_ni_node.values

    if cf_ni_values:
        # Strategy 1: Find the IS depth-1 child whose values match CF's NI
        best_match = None
        for child in is_tree.children:
            if not child.values:
                continue
            # Count how many periods match CF's NI within tolerance
            matches = 0
            total = 0
            for p, cf_val in cf_ni_values.items():
                is_val = child.values.get(p)
                if is_val is not None:
                    total += 1
                    if abs(is_val - cf_val) < 0.5:
                        matches += 1
            if total > 0 and matches == total:
                best_match = child
                break

        if best_match:
            best_match.role = "INC_NET"
            best_match.values = dict(cf_ni_values)
        else:
            # Strategy 2: Fall back to first positive-weight child
            fallback = None
            for child in is_tree.children:
                if child.weight > 0 and child.values:
                    fallback = child
                    break

            if fallback:
                print("WARNING: No IS child value-matched CF's NI — "
                      f"falling back to first positive-weight child: {fallback.name}",
                      file=sys.stderr)
                fallback.role = "INC_NET"
                fallback.values = dict(cf_ni_values)
            else:
                print("WARNING: Could not identify INC_NET in IS tree — "
                      "tagging root as fallback", file=sys.stderr)
                is_tree.role = "INC_NET"
                is_tree.values = dict(cf_ni_values)
    else:
        # No CF NI available — tag IS root as INC_NET (best guess)
        print("WARNING: No CF NI values available — tagging IS root as INC_NET",
              file=sys.stderr)
        is_tree.role = "INC_NET"

def _override_bs_cash(assets_tree: TreeNode | None, cf_endc_values: dict | None):
    """Override BS cash node values with CF_ENDC values."""
    if not assets_tree or not cf_endc_values:
        return

    cash_node = find_node_by_role(assets_tree, "BS_CASH")
    tca_node = find_node_by_role(assets_tree, "BS_TCA")

    if not cash_node:
        return

    for period, new_val in cf_endc_values.items():
        old_val = cash_node.values.get(period, 0)
        delta = new_val - old_val
        cash_node.values[period] = new_val

        # Adjust TCA subtotal to absorb the delta
        if tca_node and abs(delta) > 0.5:
            tca_node.values[period] = tca_node.values.get(period, 0) + delta

def _filter_to_complete_periods(trees: dict):
    """Remove values for periods that aren't present in ALL statements."""
    is_periods = set(trees["IS"].values.keys()) if trees.get("IS") else set()
    bs_periods = set(trees["BS"].values.keys()) if trees.get("BS") else set()
    bs_le_periods = set(trees["BS_LE"].values.keys()) if trees.get("BS_LE") else bs_periods
    cf_periods = set(trees["CF"].values.keys()) if trees.get("CF") else set()

    complete = is_periods & bs_periods & bs_le_periods & cf_periods
    trees["complete_periods"] = sorted(complete)

    if not complete:
        import sys
        print(f"WARNING: No complete periods. IS={sorted(is_periods)}, "
              f"BS={sorted(bs_periods)}, CF={sorted(cf_periods)}", file=sys.stderr)
        return

    # Walk every tree and filter values to complete periods only
    def _filter_node(node: TreeNode):
        node.values = {p: v for p, v in node.values.items() if p in complete}
        for child in node.children:
            _filter_node(child)

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _filter_node(tree)

def reconcile_trees(trees: dict) -> dict:
    """Tag key nodes by position and apply cross-statement value overrides."""
    facts = trees.get("facts", {})

    # --- Step A: Tag Balance Sheet positions ---
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))

    # --- Step B: Tag CF structural positions + find CF_ENDC ---
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)

    # --- Step C: Tag IS positions using CF's NI as authoritative ---
    _tag_is_positions(trees.get("IS"), trees.get("CF"))

    # --- Step D: Apply cross-statement value overrides ---
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    # --- Step E: Filter to complete periods ---
    _filter_to_complete_periods(trees)

    # --- Step F: Tag D&A and SBC nodes for sheet formula references ---
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    return trees

def build_statement_trees(html: str, base_url: str) -> dict:
    """Build IS/BS/CF trees from a filing's HTML and calc linkbase.

    Returns: {"IS": TreeNode, "BS": TreeNode, "CF": TreeNode,
              "facts": dict, "periods": list}
    """
    # Parse iXBRL facts
    facts = build_xbrl_facts_dict(html)

    # Fetch and parse calc linkbase
    cal_xml = fetch_cal_linkbase(html, base_url)
    if not cal_xml:
        print("ERROR: Could not find calculation linkbase", file=sys.stderr)
        return None

    all_trees = parse_calc_linkbase(cal_xml)
    stmt_roles = classify_roles(list(all_trees.keys()))

    result = {"facts": facts}
    for stmt, role in stmt_roles.items():
        calc_children = all_trees[role]
        roots = find_roots(calc_children)

        # Pick the most relevant root
        # IS: prefer NetIncomeLoss or root containing it
        # BS: prefer Assets or LiabilitiesAndStockholdersEquity
        # CF: prefer the one with CashEquivalents in name
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

        # For BS, also build the liabilities+equity tree
        if stmt == "BS":
            for r in roots:
                if r != best_root:
                    tree2 = build_tree(calc_children, facts, r)
                    result["BS_LE"] = tree2

    # Determine complete periods
    all_periods = set()
    for tag_vals in facts.values():
        all_periods.update(tag_vals.keys())
    result["periods"] = sorted(all_periods)

    # Reconcile: tag positions + apply cross-statement overrides
    reconcile_trees(result)

    return result


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
        # Derive base URL
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

            # Show groupable sibling sets
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
        for key in ["complete_periods", "periods"]:
            if key in result:
                out[key] = result[key]
        out["facts"] = result.get("facts", {})
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = result.get(stmt)
            if tree:
                out[stmt] = tree.to_dict()
                out[f"{stmt}_groupable"] = find_groupable_siblings(tree)
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
