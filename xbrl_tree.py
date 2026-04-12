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

from parse_xbrl_facts import build_xbrl_facts_dict, build_segment_facts_dict
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

def fetch_pre_linkbase(html: str, base_url: str) -> str | None:
    """Find and fetch the presentation linkbase referenced by the filing."""
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None

    schema_href = m.group(1)
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href

    schema = fetch_url(schema_href).decode('utf-8', errors='replace')

    pre_pat = re.compile(r'href="([^"]*_pre\.xml)"', re.IGNORECASE)
    pre_m = pre_pat.search(schema)
    if not pre_m:
        return None

    pre_href = pre_m.group(1)
    if not pre_href.startswith('http'):
        pre_href = base_url + pre_href

    return fetch_url(pre_href).decode('utf-8', errors='replace')


def fetch_lab_linkbase(html: str, base_url: str) -> str | None:
    """Find and fetch the label linkbase referenced by the filing."""
    schema_pat = re.compile(r'schemaRef[^>]*href="([^"]+)"', re.IGNORECASE)
    m = schema_pat.search(html)
    if not m:
        return None

    schema_href = m.group(1)
    if not schema_href.startswith('http'):
        schema_href = base_url + schema_href

    schema = fetch_url(schema_href).decode('utf-8', errors='replace')

    lab_pat = re.compile(r'href="([^"]*_lab\.xml)"', re.IGNORECASE)
    lab_m = lab_pat.search(schema)
    if not lab_m:
        return None

    lab_href = lab_m.group(1)
    if not lab_href.startswith('http'):
        lab_href = base_url + lab_href

    return fetch_url(lab_href).decode('utf-8', errors='replace')


def parse_lab_linkbase(lab_xml: str) -> dict[str, dict[str, str]]:
    """Parse label linkbase into {concept: {role_suffix: text}}.

    Returns e.g. {"us-gaap_Revenue...": {"label": "Revenue from...", "terseLabel": "Net sales"}}
    Concept keys use underscore separator (matching calc linkbase convention).
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(lab_xml)
    ns = {
        'link': 'http://www.xbrl.org/2003/linkbase',
        'xlink': 'http://www.w3.org/1999/xlink',
    }
    labels = {}
    for label_link in root.findall('.//link:labelLink', ns):
        locs = {}
        for loc in label_link.findall('link:loc', ns):
            locs[loc.get('{http://www.w3.org/1999/xlink}label')] = \
                loc.get('{http://www.w3.org/1999/xlink}href', '')
        lab_texts = {}
        for lab in label_link.findall('link:label', ns):
            role = lab.get('{http://www.w3.org/1999/xlink}role', '')
            role_suffix = role.rsplit('/', 1)[-1] if '/' in role else role
            xlink_label = lab.get('{http://www.w3.org/1999/xlink}label')
            if xlink_label not in lab_texts:
                lab_texts[xlink_label] = {}
            lab_texts[xlink_label][role_suffix] = lab.text or ''
        for arc in label_link.findall('link:labelArc', ns):
            from_label = arc.get('{http://www.w3.org/1999/xlink}from')
            to_label = arc.get('{http://www.w3.org/1999/xlink}to')
            href = locs.get(from_label, '')
            texts = lab_texts.get(to_label, {})
            if href and texts:
                # Convert href to concept key: "schema.xsd#us-gaap_Assets" -> "us-gaap_Assets"
                concept = href.split('#')[-1] if '#' in href else href
                if concept not in labels:
                    labels[concept] = {}
                labels[concept].update(texts)
    return labels


def get_label(concept_or_member: str, lab_labels: dict, prefer_terse: bool = True) -> str:
    """Get best label for a concept/member from the label linkbase.

    For member names like 'us-gaap:ProductMember', converts to underscore form
    and strips 'Member' suffix from the fallback.
    """
    # Normalize: colon form -> underscore form for lookup
    key = concept_or_member.replace(':', '_', 1) if ':' in concept_or_member else concept_or_member
    entry = lab_labels.get(key, {})
    if prefer_terse and entry.get("terseLabel"):
        return entry["terseLabel"]
    if entry.get("label"):
        return entry["label"]
    # Fallback: clean the concept/member name
    name = key.split('_', 1)[-1] if '_' in key else key
    # Strip "Member" suffix
    if name.endswith("Member"):
        name = name[:-6]
    # CamelCase to spaces
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    return name


def parse_pre_linkbase(pre_xml: str) -> dict[str, dict[str, float]]:
    """Parse presentation linkbase into {role: {concept: global_position}}.

    Uses BS4 for proper locator resolution (label → concept via xlink:href),
    then flattens the presentation tree to produce a global display ordering.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(pre_xml, 'xml')
    role_orders = {}

    for link in soup.find_all('presentationLink'):
        role = link.get('xlink:role')
        if not role:
            continue

        # Resolve locator labels to concept names
        locators = {}
        for loc in link.find_all('loc'):
            label = loc.get('xlink:label')
            href = loc.get('xlink:href', '')
            concept = href.split('#')[-1] if '#' in href else href
            if label and concept:
                locators[label] = concept

        # Build parent→children tree from arcs
        children_map = defaultdict(list)
        for arc in link.find_all('presentationArc'):
            from_label = arc.get('xlink:from')
            to_label = arc.get('xlink:to')
            order = float(arc.get('order', 0.0))
            parent_concept = locators.get(from_label)
            child_concept = locators.get(to_label)
            if parent_concept and child_concept:
                children_map[parent_concept].append((order, child_concept))

        # Sort children by order attribute
        for parent in children_map:
            children_map[parent].sort(key=lambda x: x[0])
            children_map[parent] = [c for _, c in children_map[parent]]

        # Find roots (parents that aren't children of anything)
        all_children = set()
        for clist in children_map.values():
            all_children.update(clist)
        roots = [p for p in children_map if p not in all_children]

        # Flatten tree via DFS to get global display order
        flat_order = []
        def _flatten(node):
            if node not in flat_order:
                flat_order.append(node)
            for child in children_map.get(node, []):
                _flatten(child)

        for root in roots:
            _flatten(root)

        role_orders[role] = {concept: i for i, concept in enumerate(flat_order)}

    return role_orders


def build_presentation_index(role_orders: dict, role_url: str) -> dict[str, float]:
    """Find the presentation index for a given calc role URL."""
    for role, concepts in role_orders.items():
        if role_url in role or role in role_url:
            return concepts

    cal_segment = role_url.rsplit('/', 1)[-1].upper()
    for role, concepts in role_orders.items():
        pre_segment = role.rsplit('/', 1)[-1].upper()
        if cal_segment == pre_segment:
            return concepts

    return {}

def sort_by_presentation(node, pres_index: dict[str, float]):
    if node.children:
        node.children.sort(key=lambda c: pres_index.get(c.concept, 999))
        for child in node.children:
            sort_by_presentation(child, pres_index)


def cascade_layout(tree: 'TreeNode') -> list['TreeNode']:
    """Return nodes in post-order: children before parent (IS cascade layout)."""
    result = []
    def _postorder(node):
        for child in node.children:
            _postorder(child)
        result.append(node)
    _postorder(tree)
    return result


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
                # Tag BS_CASH: prefer child whose concept matches CashAndCashEquivalent*
                cash_node = None
                for grandchild in child.children:
                    # Strip namespace prefix (colon or underscore separator)
                    bare = grandchild.concept
                    if ':' in bare:
                        bare = bare.split(':', 1)[1]
                    elif '_' in bare:
                        bare = bare.split('_', 1)[1]
                    if bare.lower().startswith("cashandcashequivalent"):
                        cash_node = grandchild
                        break
                # Fallback: first child (preserves old behavior when no cash concept)
                if cash_node is None and child.children:
                    cash_node = child.children[0]
                    print(f"WARNING: No CashAndCashEquivalent* concept found in TCA children, "
                          f"falling back to position 0 ({cash_node.concept})", file=sys.stderr)
                if cash_node:
                    cash_node.role = "BS_CASH"
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

def _find_by_keywords(tree: 'TreeNode', keywords: list[str],
                      mode: str = "all", search: str = "dfs",
                      leaf_only: bool = True, field: str = "name") -> 'TreeNode | None':
    """Unified keyword search over a TreeNode tree.

    Args:
        tree: Root node to search.
        keywords: Keywords to match (case-insensitive).
        mode: "all" = node must contain ALL keywords; "any" = at least one.
        search: "dfs" = depth-first; "bfs" = breadth-first (shallowest match).
        leaf_only: If True, skip non-leaf nodes.
        field: "name" = search node.name; "concept" = search node.concept.
    """
    match_fn = all if mode == "all" else any

    def _matches(node):
        if leaf_only and not node.is_leaf:
            return False
        text = getattr(node, field, "").lower()
        return match_fn(kw in text for kw in keywords)

    if search == "bfs":
        from collections import deque
        queue = deque([tree])
        while queue:
            node = queue.popleft()
            if _matches(node):
                return node
            for child in node.children:
                queue.append(child)
        return None
    else:  # dfs
        if _matches(tree):
            return tree
        for child in tree.children:
            result = _find_by_keywords(child, keywords, mode=mode, search=search,
                                       leaf_only=leaf_only, field=field)
            if result:
                return result
        return None

def _tag_is_semantic(is_tree: 'TreeNode') -> None:
    """Tag IS_REVENUE and IS_COGS by BFS keyword + value matching.

    Strategy: find COGS first (unambiguous keywords), then find Revenue
    as the largest-value sibling or near-sibling that contains revenue/sales
    keywords but NOT cost keywords. Falls back to value-based detection:
    the largest positive-valued node at the same depth as COGS.
    """
    if not is_tree:
        return
    cogs_keywords = ["costofgoods", "costofrevenue", "costofsales"]
    rev_keywords = ["revenue", "sales"]

    # Tag COGS first (unambiguous)
    cogs_node = _find_by_keywords(is_tree, cogs_keywords, mode="any", search="bfs", leaf_only=False, field="concept")
    if cogs_node:
        cogs_node.role = "IS_COGS"

    # Find Revenue: exclude nodes whose concept also matches COGS keywords
    from collections import deque
    queue = deque([is_tree])
    while queue:
        node = queue.popleft()
        concept_lower = node.concept.lower()
        is_rev = any(kw in concept_lower for kw in rev_keywords)
        is_cost = any(kw in concept_lower for kw in cogs_keywords)
        if is_rev and not is_cost:
            node.role = "IS_REVENUE"
            return
        for child in node.children:
            queue.append(child)

    # Fallback: if COGS found, Revenue is likely its sibling with the largest values
    if cogs_node:
        parent = _find_parent(is_tree, cogs_node)
        if parent:
            best, best_avg = None, 0
            for child in parent.children:
                if child is cogs_node or child.role:
                    continue
                avg = sum(abs(v) for v in child.values.values()) / max(len(child.values), 1)
                if avg > best_avg:
                    best, best_avg = child, avg
            if best:
                best.role = "IS_REVENUE"


def _find_parent(tree: TreeNode, target: TreeNode) -> TreeNode | None:
    """Find the parent of target node in the tree."""
    for child in tree.children:
        if child is target:
            return tree
        result = _find_parent(child, target)
        if result:
            return result
    return None


def _supplement_orphan_facts(parent: 'TreeNode', orphan_facts: dict, used_tags: set) -> None:
    """Fill tree gaps with unused XBRL facts. Bottom-up, never mutates existing .values."""
    for child in list(parent.children):
        _supplement_orphan_facts(child, orphan_facts, used_tags)
    if not parent.children or not parent.values:
        return
    for concept, values in list(orphan_facts.items()):
        if concept in used_tags:
            continue
        closes_gap = True
        periods_checked = 0
        for period in parent.values:
            parent_val = parent.values.get(period, 0)
            children_sum = sum(c.values.get(period, 0) * c.weight for c in parent.children)
            gap = parent_val - children_sum
            orphan_val = values.get(period, 0)
            if orphan_val == 0 and gap == 0:
                continue
            periods_checked += 1
            if abs(gap - orphan_val) > 0.5:
                closes_gap = False
                break
        if closes_gap and periods_checked > 0:
            new_node = TreeNode(concept, weight=1.0)
            new_node.values = dict(values)
            parent.add_child(new_node)
            used_tags.add(concept)


def _supplement_orphan_facts_all(trees: dict) -> None:
    """Run orphan fact supplementation across all statement trees."""
    facts = trees.get("facts", {})
    if not facts:
        return
    # Collect all concepts already used in any tree
    used_tags = set()
    def _collect(node):
        used_tags.add(node.concept)
        if node.tag:
            used_tags.add(node.tag)
        for c in node.children:
            _collect(c)
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _collect(tree)
    # Supplement each tree
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _supplement_orphan_facts(tree, facts, used_tags)


def verify_tree_completeness(tree: 'TreeNode', periods: list[str]) -> list:
    """Check SUM(children * weight) == declared for all branch nodes. Returns list of errors."""
    errors = []
    def _check(node):
        if not node.children:
            return
        for period in periods:
            declared = node.values.get(period, 0)
            if declared == 0:
                continue
            computed = sum(c.values.get(period, 0) * c.weight for c in node.children)
            gap = declared - computed
            if abs(gap) > 1.0:
                errors.append((node.concept, period, gap))
        for child in node.children:
            _check(child)
    _check(tree)
    return errors


def merge_calc_pres(tree: 'TreeNode', pres_index: dict[str, float],
                    periods: list[str]) -> 'TreeNode':
    """Merge calc tree with presentation ordering. Adds 'Other' rows for gaps.

    Three-layer approach:
      1. CALC tree — mathematical truth (parent→children with weights)
      2. PRES order — display ordering from presentation linkbase
      3. "Other" — residual = declared_parent - SUM(known children)

    Recurses bottom-up so children are fixed before parent's gap is computed.
    Returns the same tree, mutated in place.
    """
    # Recurse into children first (bottom-up)
    for child in list(tree.children):
        merge_calc_pres(child, pres_index, periods)

    if not tree.children:
        return tree  # Leaf — nothing to do

    # --- Partition children ---
    presented = []
    unpresented = []
    for child in tree.children:
        if child.concept in pres_index:
            presented.append(child)
        else:
            unpresented.append(child)

    # Sort presented children by presentation order
    presented.sort(key=lambda c: pres_index.get(c.concept, 999))

    # Reorder: presented first, then unpresented (original calc order preserved)
    tree.children = presented + unpresented

    # --- Compute residual per period ---
    residual_values = {}
    has_nonzero_residual = False
    for period in periods:
        declared = tree.values.get(period, 0)
        if declared == 0:
            continue
        computed = sum(c.values.get(period, 0) * c.weight for c in tree.children)
        gap = declared - computed
        residual_values[period] = gap
        if abs(gap) > 1.0:
            has_nonzero_residual = True

    # --- Insert "Other" node if there's a gap ---
    if has_nonzero_residual:
        other = TreeNode(f"__OTHER__{tree.concept}", weight=1.0)
        other.name = "Other"
        other.values = residual_values
        other.is_leaf = True
        tree.add_child(other)

    return tree


CROSS_STATEMENT_CHECKS = [
    {"name": "BS Balance (TA-TL-TE)", "roles": ["BS_TA", "BS_TL", "BS_TE"], "formula": "={BS_TA}-{BS_TL}-{BS_TE}"},
    {"name": "Cash Link (CF_ENDC-BS_CASH)", "roles": ["CF_ENDC", "BS_CASH"], "formula": "={left}-{right}"},
    {"name": "NI Link (IS-CF)", "roles": ["INC_NET", "INC_NET_CF"], "formula": "={left}-{right}"},
    {"name": "D&A Link (IS-CF)", "roles": ["IS_DA", "CF_DA"], "formula": "={left}-{right}"},
    {"name": "SBC Link (IS-CF)", "roles": ["IS_SBC", "CF_SBC"], "formula": "={left}-{right}"},
]


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
    is_da = _find_by_keywords(is_tree, ["depreciation"], mode="all", search="dfs", leaf_only=True, field="name")
    if not is_da:
        is_da = _find_by_keywords(is_tree, ["amortization"], mode="all", search="dfs", leaf_only=True, field="name")
    
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
    is_sbc = _find_by_keywords(is_tree, ["stock", "compensation"], mode="all", search="dfs", leaf_only=True, field="name")
    if not is_sbc:
        is_sbc = _find_by_keywords(is_tree, ["share", "compensation"], mode="all", search="dfs", leaf_only=True, field="name")
    
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

    # Look up CF_ENDC from XBRL facts (instant context, not in any tree).
    # Strategy: derive from the CF root concept. The root is the "PeriodIncrease
    # Decrease" version; the ending cash balance is the same base concept without
    # that suffix. Fall back to common tags if derivation fails.
    if facts and cf_tree:
        # Try to derive from CF root concept
        root_tag = cf_tree.tag  # e.g. "us-gaap:CashCash...PeriodIncreaseDecreaseIncludingExchangeRateEffect"
        # Strip the PeriodIncrease... suffix to get the balance concept
        derived = re.sub(r'PeriodIncreaseDecrease.*$', '', root_tag)
        if derived != root_tag and derived in facts:
            cf_endc_values = facts[derived]

    if not cf_endc_values and facts:
        endc_tags = [
            "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
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
    fx_found = False
    for child in cf_tree.children:
        concept_name = child.concept.split('_', 1)[-1] if '_' in child.concept else child.concept
        for pat in FX_PATTERNS:
            if concept_name.startswith(pat) and child.values:
                child.role = "CF_FX"
                fx_found = True
                break

    # If the CF root is the "ExcludingExchangeRateEffect" variant, FX lives
    # outside the calc linkbase tree.  Look it up in facts and inject a
    # synthetic child so NETCH = OPCF + INVCF + FINCF + FX holds.
    if not fx_found and facts and "Excluding" in (cf_tree.tag or ""):
        FX_FACT_TAGS = [
            "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "us-gaap:EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
            "us-gaap:EffectOfExchangeRateOnCashAndCashEquivalents",
        ]
        for tag in FX_FACT_TAGS:
            if tag in facts and facts[tag]:
                fx_node = TreeNode(tag)
                fx_node.concept = tag.replace(":", "_")
                fx_node.tag = tag
                fx_node.values = dict(facts[tag])
                fx_node.role = "CF_FX"
                cf_tree.add_child(fx_node)
                # Update the CF root values to include FX (Excluding → true NETCH)
                for period, fx_val in facts[tag].items():
                    cf_tree.values[period] = cf_tree.values.get(period, 0) + fx_val
                fx_found = True
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

    # Note: IS_REVENUE and IS_COGS are now tagged by _tag_is_semantic() in reconcile_trees()

    # Find CF's NI values (the authoritative source)
    cf_ni_values = None
    if cf_tree:
        cf_ni_node = find_node_by_role(cf_tree, "INC_NET_CF")
        if cf_ni_node:
            cf_ni_values = cf_ni_node.values

    if cf_ni_values:
        # Strategy 0: Check if the IS root itself matches CF's NI
        best_match = None
        root_matches = 0
        root_total = 0
        for p, cf_val in cf_ni_values.items():
            is_val = is_tree.values.get(p)
            if is_val is not None:
                root_total += 1
                if abs(is_val - cf_val) < 0.5:
                    root_matches += 1
        if root_total > 0 and root_matches == root_total:
            best_match = is_tree

        # Strategy 1: depth-1 child search (only if root didn't match)
        if not best_match:
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
            else:
                print("WARNING: Could not identify INC_NET in IS tree — "
                      "tagging root as fallback", file=sys.stderr)
                is_tree.role = "INC_NET"
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

# ---------------------------------------------------------------------------
# Segment Decomposition
# ---------------------------------------------------------------------------

# Dimension priority order for IS segment breakdowns
_SEGMENT_DIM_PRIORITY = [
    "srt:ProductOrServiceAxis",
    "us-gaap:StatementBusinessSegmentsAxis",
]


def _find_best_decomposition(members_data: dict, total_values: dict,
                              periods: list[str]) -> list[str] | None:
    """Find the largest subset of members that sums to total across all periods.

    Returns list of member keys, or None if no valid decomposition found.
    """
    from itertools import combinations

    member_names = list(members_data.keys())
    if not member_names or not total_values:
        return None

    def subset_sums_to_total(subset):
        for p in periods:
            t = total_values.get(p)
            if t is None:
                continue
            s = sum(members_data[m].get(p, 0) for m in subset)
            if abs(s - t) > 0.5:
                return False
        return True

    # Try from largest subset down to size 2
    for size in range(len(member_names), 1, -1):
        for subset in combinations(member_names, size):
            if subset_sums_to_total(subset):
                return list(subset)
    return None


def _detect_segments_for_node(node: TreeNode, seg_facts: dict,
                               periods: list[str]) -> tuple[list[str], dict] | None:
    """Detect the best segment decomposition for a leaf tree node.

    Tries dimensions in priority order. For each, finds the largest subset
    of members that sums to the node's total values.

    Returns (leaf_members, {member: {period: value}}) or None.
    """
    tag = node.tag
    tag_segs = seg_facts.get(tag, {})
    if not tag_segs:
        return None

    for dim in _SEGMENT_DIM_PRIORITY:
        members = tag_segs.get(dim)
        if not members:
            continue
        leaves = _find_best_decomposition(members, node.values, periods)
        if leaves:
            leaf_data = {m: members[m] for m in leaves}
            return leaves, leaf_data

    return None


def _attach_segment_children(node: TreeNode, leaf_members: list[str],
                              member_values: dict, lab_labels: dict,
                              periods: list[str]):
    """Convert a leaf node into a parent with segment children."""
    period_set = set(periods)
    for member in leaf_members:
        child = TreeNode(member.replace(':', '_', 1), weight=1.0)
        child.name = get_label(member, lab_labels)
        # Filter to complete periods only
        child.values = {p: v for p, v in member_values[member].items() if p in period_set}
        child.is_leaf = True
        child.role = None
        node.add_child(child)
    # Node is now a parent — is_leaf was set to False by add_child


def _attach_is_segments(trees: dict, seg_facts: dict, lab_labels: dict):
    """Attach segment breakdowns to IS Revenue and COGS nodes.

    For each node with role IS_REVENUE or IS_COGS:
    1. Try dimensions in priority order
    2. Verify leaves sum to total
    3. Prefer the dimension that works for BOTH Rev and COGS (shared margins)
    4. Attach as children
    """
    is_tree = trees.get("IS")
    if not is_tree:
        return

    periods = trees.get("complete_periods", [])
    if not periods:
        return

    # Find Rev and COGS nodes — if the tagged node has calc children,
    # try to attach segments to its leaf children instead
    def _collect_segment_targets(node):
        if not node:
            return []
        if node.is_leaf:
            return [node]
        # Node has calc children — collect leaf descendants
        leaves = []
        for child in node.children:
            if child.is_leaf:
                leaves.append(child)
        return leaves

    rev_node = find_node_by_role(is_tree, "IS_REVENUE")
    cogs_node = find_node_by_role(is_tree, "IS_COGS")
    rev_targets = _collect_segment_targets(rev_node)
    cogs_targets = _collect_segment_targets(cogs_node)
    targets = rev_targets + cogs_targets

    if not targets:
        return

    # Strategy: find the best shared dimension first
    # Check which dimensions work for both a rev leaf and a cogs leaf
    shared_done = set()
    if rev_targets and cogs_targets:
        for dim in _SEGMENT_DIM_PRIORITY:
            for rt in rev_targets:
                for ct in cogs_targets:
                    rev_members = seg_facts.get(rt.tag, {}).get(dim)
                    cogs_members = seg_facts.get(ct.tag, {}).get(dim)
                    if not rev_members or not cogs_members:
                        continue
                    rev_leaves = _find_best_decomposition(rev_members, rt.values, periods)
                    cogs_leaves = _find_best_decomposition(cogs_members, ct.values, periods)
                    if rev_leaves and cogs_leaves:
                        print(f"  Segments: shared {dim} — "
                              f"Revenue ({len(rev_leaves)} segments), "
                              f"COGS ({len(cogs_leaves)} segments)", file=sys.stderr)
                        _attach_segment_children(rt, rev_leaves,
                                                 {m: rev_members[m] for m in rev_leaves}, lab_labels, periods)
                        _attach_segment_children(ct, cogs_leaves,
                                                 {m: cogs_members[m] for m in cogs_leaves}, lab_labels, periods)
                        shared_done.update([id(rt), id(ct)])
            if shared_done:
                break  # found a shared dimension, stop
    # Remove already-handled nodes from targets
    targets = [t for t in targets if id(t) not in shared_done]

    # No shared dimension — decompose each independently
    for node in targets:
        result = _detect_segments_for_node(node, seg_facts, periods)
        if result:
            leaf_members, member_values = result
            role_label = "Revenue" if node.role == "IS_REVENUE" else "COGS"
            print(f"  Segments: {role_label} → {len(leaf_members)} segments",
                  file=sys.stderr)
            _attach_segment_children(node, leaf_members, member_values, lab_labels, periods)


def _build_revenue_segment_tree(trees: dict, seg_facts: dict,
                                 multi_seg_facts: dict,
                                 lab_labels: dict) -> TreeNode | None:
    """Build a hierarchical revenue segment tree for forecasting.

    Architecture:
    1. Outer level: BusinessSegmentsAxis (1D) — verified sum ≈ total
    2. Inner level: ProductOrServiceAxis × BusinessSegmentsAxis (2D)
       filtered to each outer segment — verified sum = segment total
    3. Residual "Other/Eliminations" node if outer sum ≠ total

    Returns a TreeNode tree, or None if no useful segments found.
    """
    is_tree = trees.get("IS")
    if not is_tree:
        return None
    periods = trees.get("complete_periods", [])
    if not periods:
        return None
    period_set = set(periods)

    # Find the revenue tag(s) — collect from IS_REVENUE node and its children
    rev_node = find_node_by_role(is_tree, "IS_REVENUE")
    if not rev_node:
        return None

    # Collect candidate revenue tags — from IS tree + common GAAP revenue tags
    rev_tags = set()
    def _collect_tags(node):
        rev_tags.add(node.tag)
        for child in node.children:
            _collect_tags(child)
    _collect_tags(rev_node)
    # Also check common revenue tags that may have segment data
    # even if the IS tree uses a different aggregation tag
    rev_tags.update([
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:SalesRevenueNet",
    ])

    # Get total revenue values from the IS tree
    total_values = {p: v for p, v in rev_node.values.items() if p in period_set}
    if not total_values:
        return None

    # --- Try BusinessSegmentsAxis as outer level ---
    biz_dim = "us-gaap:StatementBusinessSegmentsAxis"
    prod_dim = "srt:ProductOrServiceAxis"

    # Find 1D business segments for any revenue tag
    biz_members = None
    biz_tag = None
    for tag in rev_tags:
        tag_segs = seg_facts.get(tag, {})
        members = tag_segs.get(biz_dim)
        if not members:
            continue
        # Check if these segments approximately sum to total (allow small hedging gap)
        member_sum = {}
        for m_vals in members.values():
            for p, v in m_vals.items():
                if p in period_set:
                    member_sum[p] = member_sum.get(p, 0) + v
        # Allow up to 1% gap (hedging/eliminations)
        all_close = all(
            abs(member_sum.get(p, 0) - total_values.get(p, 0)) / max(abs(total_values[p]), 1) < 0.01
            for p in periods if total_values.get(p, 0) != 0
        )
        if all_close and len(members) >= 2:
            biz_members = members
            biz_tag = tag
            break

    if not biz_members:
        # No business segments — try ProductOrServiceAxis as outer level
        prod_members = None
        for tag in rev_tags:
            tag_segs = seg_facts.get(tag, {})
            members = tag_segs.get(prod_dim)
            if not members or len(members) < 2:
                continue
            leaves = _find_best_decomposition(members, total_values, periods)
            if leaves and len(leaves) >= 2:
                prod_members = {m: members[m] for m in leaves}
                break

        if not prod_members:
            return None

        # Build flat tree from ProductOrServiceAxis leaves
        root = TreeNode("_REVENUE_SEGMENTS", weight=1.0)
        root.name = "Revenue Segments"
        root.values = dict(total_values)
        root.is_leaf = False
        for member, vals in sorted(prod_members.items(),
                                    key=lambda x: -sum(abs(v) for v in x[1].values())):
            child = TreeNode(member.replace(':', '_', 1), weight=1.0)
            child.name = get_label(member, lab_labels)
            child.values = {p: v for p, v in vals.items() if p in period_set}
            child.is_leaf = True
            root.add_child(child)
        print(f"  Revenue segments: {len(root.children)} products "
              f"(ProductOrServiceAxis)", file=sys.stderr)
        return root

    # Build the outer tree: Revenue → segments
    root = TreeNode("_REVENUE_SEGMENTS", weight=1.0)
    root.name = "Revenue Segments"
    root.values = dict(total_values)
    root.is_leaf = False

    # Find 2D product×segment facts
    prod_biz_dims = tuple(sorted([prod_dim, biz_dim]))
    multi_2d = {}
    for tag in rev_tags:
        tag_multi = multi_seg_facts.get(tag, {})
        if prod_biz_dims in tag_multi:
            multi_2d = tag_multi[prod_biz_dims]
            break

    # Determine dimension ordering in the tuple key
    dim_order = list(prod_biz_dims)
    prod_idx = dim_order.index(prod_dim)
    biz_idx = dim_order.index(biz_dim)

    seg_sum = {p: 0.0 for p in periods}
    for seg_member, seg_vals in sorted(biz_members.items(),
                                        key=lambda x: -sum(abs(v) for v in x[1].values())):
        seg_node = TreeNode(seg_member.replace(':', '_', 1), weight=1.0)
        seg_node.name = get_label(seg_member, lab_labels)
        seg_node.values = {p: v for p, v in seg_vals.items() if p in period_set}

        for p in periods:
            seg_sum[p] += seg_node.values.get(p, 0)

        # Find 2D product members within this segment
        inner_members = {}
        for member_tuple, vals in multi_2d.items():
            if member_tuple[biz_idx] == seg_member:
                prod_member = member_tuple[prod_idx]
                inner_members[prod_member] = {p: v for p, v in vals.items() if p in period_set}

        if inner_members:
            # Apply decomposition: find leaves that sum to segment total
            leaves = _find_best_decomposition(inner_members, seg_node.values, periods)
            if leaves:
                for leaf_member in leaves:
                    child = TreeNode(leaf_member.replace(':', '_', 1), weight=1.0)
                    child.name = get_label(leaf_member, lab_labels)
                    child.values = dict(inner_members[leaf_member])
                    child.is_leaf = True
                    seg_node.add_child(child)
                print(f"  Revenue segments: {seg_node.name} → "
                      f"{len(leaves)} products", file=sys.stderr)
            else:
                seg_node.is_leaf = True
        else:
            seg_node.is_leaf = True

        root.add_child(seg_node)

    # Add residual node if segments don't exactly sum to total
    for p in periods:
        gap = total_values.get(p, 0) - seg_sum.get(p, 0)
        if abs(gap) > 0.5:
            elim_node = TreeNode("_ELIMINATIONS", weight=1.0)
            elim_node.name = "Hedging & Eliminations"
            elim_node.values = {
                p: total_values.get(p, 0) - seg_sum.get(p, 0)
                for p in periods
            }
            elim_node.is_leaf = True
            root.add_child(elim_node)
            print(f"  Revenue segments: added Hedging & Eliminations", file=sys.stderr)
            break

    # Only return if we have meaningful segments (2+ children)
    if len(root.children) >= 2:
        return root
    return None


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

def reconcile_trees(trees: dict, pres_index: dict | None = None) -> dict:
    """Tag key nodes by position and apply cross-statement value overrides."""
    facts = trees.get("facts", {})

    # --- Step A: Tag Balance Sheet positions ---
    _tag_bs_positions(trees.get("BS"), trees.get("BS_LE"))

    # --- Step B: Tag CF structural positions + find CF_ENDC ---
    cf_endc_values = _tag_cf_positions(trees.get("CF"), facts)

    # --- Step C: Tag IS positions using CF's NI as authoritative ---
    _tag_is_positions(trees.get("IS"), trees.get("CF"))

    # --- Step D: Tag IS Revenue and COGS by keyword BFS ---
    _tag_is_semantic(trees.get("IS"))

    # --- Step E: Apply cross-statement value overrides ---
    _override_bs_cash(trees.get("BS"), cf_endc_values)

    # Persist cf_endc_values so sheet_builder can build Beginning/Ending Cash rows
    if cf_endc_values:
        trees["cf_endc_values"] = cf_endc_values

    # --- Step F: Filter to complete periods ---
    _filter_to_complete_periods(trees)

    # --- Step G: Merge calc trees with presentation ordering + Other rows ---
    periods = trees.get("complete_periods", [])
    if periods:
        for stmt in ["IS", "BS", "BS_LE", "CF"]:
            tree = trees.get(stmt)
            if tree:
                stmt_pres = pres_index.get(stmt, {}) if pres_index else {}
                merge_calc_pres(tree, stmt_pres, periods)

    # --- Step H: Tag D&A and SBC nodes for sheet formula references ---
    _tag_da_sbc_nodes(trees.get("IS"), trees.get("CF"))

    return trees

def build_statement_trees(html: str, base_url: str) -> dict:
    """Build IS/BS/CF trees from a filing's HTML and calc linkbase.

    Returns: {"IS": TreeNode, "BS": TreeNode, "CF": TreeNode,
              "facts": dict, "periods": list}
    """
    # Parse iXBRL facts
    facts, unit_label = build_xbrl_facts_dict(html)

    # Fetch and parse calc linkbase
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

    # Fetch and parse pre linkbase
    pre_xml = fetch_pre_linkbase(html, base_url)
    role_orders = parse_pre_linkbase(pre_xml) if pre_xml else {}

    pres_index = {}
    for stmt, role in stmt_roles.items():
        pres_index[stmt] = build_presentation_index(role_orders, role)
        if stmt == "BS":
            pres_index["BS_LE"] = pres_index["BS"]

    # Determine complete periods
    all_periods = set()
    for tag_vals in facts.values():
        all_periods.update(tag_vals.keys())
    result["periods"] = sorted(all_periods)

    # Reconcile: sort, tag positions + apply cross-statement overrides
    reconcile_trees(result, pres_index)

    # --- Segment decomposition for Revenue/COGS ---
    seg_facts, multi_seg_facts = build_segment_facts_dict(html)
    lab_xml = fetch_lab_linkbase(html, base_url)
    lab_labels = parse_lab_linkbase(lab_xml) if lab_xml else {}
    result["lab_labels"] = lab_labels
    _attach_is_segments(result, seg_facts, lab_labels)

    # --- Build hierarchical revenue segment tree ---
    rev_segments = _build_revenue_segment_tree(
        result, seg_facts, multi_seg_facts, lab_labels)
    if rev_segments:
        result["revenue_segments"] = rev_segments

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
