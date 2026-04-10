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
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


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
