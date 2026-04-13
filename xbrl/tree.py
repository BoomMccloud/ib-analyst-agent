import re
import sys

def _concept_to_tag(concept: str) -> str:
    parts = concept.split('_', 1)
    if len(parts) == 2:
        return f"{parts[0]}:{parts[1]}"
    return concept

def _clean_name(concept: str) -> str:
    name = concept.split('_', 1)[-1]
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    for suffix in ['AtCarryingValue', 'Net Current', 'Noncurrent']:
        name = name.replace(suffix, '')
    return name.strip()

class TreeNode:
    def __init__(self, concept: str, weight: float = 1.0):
        self.concept = concept
        self.tag = _concept_to_tag(concept)
        self.name = _clean_name(concept)
        self.weight = weight
        self.children: list['TreeNode'] = []
        self.values: dict[str, float] = {}
        self.is_leaf = True
        self.role: str | None = None

    def add_child(self, child: 'TreeNode'):
        self.children.append(child)
        self.is_leaf = False

    @property
    def has_groupable_children(self) -> bool:
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
    visited = set()
    def _build(concept, weight=1.0):
        if concept in visited:
            return None
        visited.add(concept)
        node = TreeNode(concept, weight)
        tag = _concept_to_tag(concept)
        if tag in facts:
            node.values = dict(facts[tag])
        kids = calc_children.get(concept, [])
        for child_concept, child_weight in kids:
            child_node = _build(child_concept, child_weight)
            if child_node:
                node.add_child(child_node)
        return node
    return _build(root_concept)

def find_roots(calc_children: dict) -> list[str]:
    all_parents = set(calc_children.keys())
    all_children = set(c for kids in calc_children.values() for c, _ in kids)
    return sorted(all_parents - all_children)

def find_groupable_siblings(tree: TreeNode) -> list[dict]:
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
        for child in node.branch_children:
            _walk(child)
    _walk(tree)
    return groups

def build_presentation_index(role_orders: dict, role_url: str) -> dict[str, float]:
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
    result = []
    def _postorder(node):
        for child in node.children:
            _postorder(child)
        result.append(node)
    _postorder(tree)
    return result

def print_tree(node: TreeNode, indent: int = 0, periods: list[str] = None):
    if periods is None:
        all_periods = set()
        def _collect(n):
            all_periods.update(n.values.keys())
            for c in n.children:
                _collect(c)
        _collect(node)
        periods = sorted(all_periods)[-3:]
    sign = {1.0: "+", -1.0: "-"}.get(node.weight, "?")
    prefix = f"{'  ' * indent}{sign} " if indent > 0 else ""
    name = node.name[:40]
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

def find_node_by_role(tree: TreeNode, role: str) -> TreeNode | None:
    if getattr(tree, "role", None) == role:
        return tree
    for child in tree.children:
        result = find_node_by_role(child, role)
        if result:
            return result
    return None

def _find_parent(tree: TreeNode, target: TreeNode) -> TreeNode | None:
    for child in tree.children:
        if child is target:
            return tree
        result = _find_parent(child, target)
        if result:
            return result
    return None

def _supplement_orphan_facts(parent: 'TreeNode', orphan_facts: dict, used_tags: set) -> None:
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
    facts = trees.get("facts", {})
    if not facts:
        return
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
    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _supplement_orphan_facts(tree, facts, used_tags)

def _filter_to_complete_periods(trees: dict):
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

    def _filter_node(node: TreeNode):
        node.values = {p: v for p, v in node.values.items() if p in complete}
        for child in node.children:
            _filter_node(child)

    for stmt in ["IS", "BS", "BS_LE", "CF"]:
        tree = trees.get(stmt)
        if tree:
            _filter_node(tree)
