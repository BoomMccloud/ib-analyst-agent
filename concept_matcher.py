import logging
import sys
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class ConceptMap:
    """A unified dictionary mapping every concept/period to its canonical name and authoritative value."""
    def __init__(self):
        # Maps old concept name to the canonical (base) concept name
        self.renames: Dict[str, str] = {}
        # Stores all collected values for each concept: concept -> {period: value}
        self.all_values: Dict[str, Dict[str, float]] = {}
        # Stores parent maps for each filing (index -> {child: parent})
        self.parent_maps: List[Dict[str, str]] = []


class ConceptMatcher:
    def align_statement(self, statement_name: str, filing_trees: Dict[int, Dict[str, Any]], all_data: List[Dict[str, Any]]) -> ConceptMap:
        cmap = ConceptMap()
        
        # Pass 1: Collect all concepts+values from all filings
        for i in range(len(all_data)):
            old_tree = filing_trees[i].get(statement_name)
            if not old_tree:
                cmap.parent_maps.append({})
                continue
            self._collect_all_concepts(old_tree, cmap.all_values)
            cmap.parent_maps.append(self._build_concept_to_parent(old_tree))

        # Pass 2: Build rename maps using overlapping periods
        all_renames = {}
        for i in range(1, len(all_data)):
            old_tree = filing_trees[i].get(statement_name)
            if not old_tree:
                continue
            older_periods = all_data[i].get("complete_periods", [])
            prev_periods = all_data[i-1].get("complete_periods", [])
            overlap = set(prev_periods) & set(older_periods)
            if not overlap:
                continue
            overlap_period = max(overlap)
            prev_tree = filing_trees[i-1].get(statement_name)
            if prev_tree:
                renames = self._build_rename_map(prev_tree, old_tree, overlap_period)
                # Chain renames: if A→B and B→C, then A→C
                for old_c, new_c in renames.items():
                    final = new_c
                    while final in all_renames:
                        final = all_renames[final]
                    all_renames[old_c] = final
                if renames:
                    print(f"  {statement_name}: renames at {overlap_period}: "
                          f"{', '.join(f'{k.split(chr(95),1)[-1][:30]}→{v.split(chr(95),1)[-1][:30]}' for k,v in renames.items())}",
                          file=sys.stderr)
        
        cmap.renames = all_renames
        return cmap

    def merge_values_by_concept(self, base_node, cmap: ConceptMap):
        """Fill in base_node's values from the all_values dict, handling renames."""
        concept = base_node.concept
        if not concept.startswith("__OTHER__"):
            # Direct concept match
            if concept in cmap.all_values:
                for p, v in cmap.all_values[concept].items():
                    if p not in base_node.values:
                        base_node.values[p] = v
            # Check if any old concept renames to this one
            for old_concept, new_concept in cmap.renames.items():
                if new_concept == concept and old_concept in cmap.all_values:
                    for p, v in cmap.all_values[old_concept].items():
                        if p not in base_node.values:
                            base_node.values[p] = v
        # Recurse
        for c in base_node.children:
            self.merge_values_by_concept(c, cmap)

    def _collect_all_concepts(self, tree, period_values=None):
        """Collect {concept: {period: value}} from a tree, recursively."""
        if period_values is None:
            period_values = {}
        if tree.concept not in period_values:
            period_values[tree.concept] = {}
        for p, v in tree.values.items():
            if v != 0 and p not in period_values[tree.concept]:
                period_values[tree.concept][p] = v
        for c in tree.children:
            self._collect_all_concepts(c, period_values)
        return period_values

    def _build_concept_to_parent(self, tree, mapping=None):
        """Build {child_concept: parent_concept} mapping."""
        if mapping is None:
            mapping = {}
        for c in tree.children:
            mapping[c.concept] = tree.concept
            self._build_concept_to_parent(c, mapping)
        return mapping

    def _build_value_index(self, tree, period):
        """Build {value: [node]} index for a tree at a given period."""
        index = {}
        def _walk(n):
            if not n.concept.startswith("__OTHER__"):
                val = n.values.get(period, 0)
                if val != 0:
                    index.setdefault(val, []).append(n)
            for c in n.children:
                _walk(c)
        _walk(tree)
        return index

    def _build_rename_map(self, base_tree, old_tree, overlap_period):
        """Build {old_concept: new_concept} mapping using value matching."""
        renames = {}
        base_index = self._build_value_index(base_tree, overlap_period)
        base_concepts = set()
        def _collect(n):
            base_concepts.add(n.concept)
            for c in n.children:
                _collect(c)
        _collect(base_tree)

        def _walk_old(n):
            if not n.concept.startswith("__OTHER__") and n.concept not in base_concepts:
                val = n.values.get(overlap_period, 0)
                if val != 0:
                    candidates = base_index.get(val, [])
                    if len(candidates) == 1:
                        renames[n.concept] = candidates[0].concept
            for c in n.children:
                _walk_old(c)
        _walk_old(old_tree)
        return renames

    def detect_and_fix_structural_shifts(self, tree, periods, statement_name=""):
        """Detect and fix structural reclassifications (parent-child promotion, sibling replacement)."""
        detections = []

        def _has_real_children(node):
            return any(not c.concept.startswith("__OTHER__") for c in node.children)

        def _check_pair(node_a, node_b, relationship):
            shared_periods = set(node_a.values.keys()) & set(node_b.values.keys())
            for p in shared_periods:
                if node_a.values[p] == 0:
                    continue
                if abs(node_a.values[p] - node_b.values[p]) >= 1.0:
                    continue
                b_only = set(node_b.values.keys()) - set(node_a.values.keys())
                a_only = set(node_a.values.keys()) - set(node_b.values.keys())
                if b_only:
                    old_node, new_node = node_a, node_b
                elif a_only:
                    old_node, new_node = node_b, node_a
                else:
                    continue
                # For siblings, skip if old node has its own subtree (not a simple replacement)
                if relationship == "sibling" and _has_real_children(old_node):
                    continue
                detections.append({
                    "old_node": old_node,
                    "new_node": new_node,
                    "relationship": relationship,
                    "overlap_period": p,
                })
                return

        def _scan(node, is_root=False):
            if node.concept.startswith("__OTHER__"):
                return
            for child in node.children:
                if child.concept.startswith("__OTHER__"):
                    continue
                if not is_root:
                    _check_pair(node, child, "parent_child")
                _scan(child)

            real_children = [c for c in node.children if not c.concept.startswith("__OTHER__")]
            for i in range(len(real_children)):
                for j in range(i + 1, len(real_children)):
                    _check_pair(real_children[i], real_children[j], "sibling")

        _scan(tree, is_root=True)

        # Phase 2: Apply fixes
        fixes_applied = 0
        for det in detections:
            old_node = det["old_node"]
            new_node = det["new_node"]

            for period, value in old_node.values.items():
                if period not in new_node.values:
                    new_node.values[period] = value

            if old_node.role and not new_node.role:
                new_node.role = old_node.role

            if det["relationship"] == "parent_child":
                self._remove_from_tree(tree, new_node)
                self._replace_in_tree(tree, old_node, new_node)
            else:
                # Sibling replacement: just remove old_node from its parent
                self._remove_from_tree(tree, old_node)
            fixes_applied += 1

            logger.info(
                "Reclassification fix: %s -> %s (%s, overlap=%s)",
                old_node.concept, new_node.concept,
                det["relationship"], det["overlap_period"],
            )
            
        if fixes_applied > 0 and statement_name:
            print(f"  {statement_name}: {fixes_applied} reclassification fix(es) applied",
                  file=sys.stderr)

        return {"detections": detections, "fixes_applied": fixes_applied}

    def _remove_from_tree(self, root, target_node, _visited=None):
        if _visited is None:
            _visited = set()
        if id(root) in _visited:
            return False
        _visited.add(id(root))
        for i, child in enumerate(root.children):
            if child is target_node:
                root.children.pop(i)
                return True
            if self._remove_from_tree(child, target_node, _visited):
                return True
        return False

    def _replace_in_tree(self, root, old_node, new_node, _visited=None):
        if _visited is None:
            _visited = set()
        if id(root) in _visited:
            return False
        _visited.add(id(root))
        for i, child in enumerate(root.children):
            if child is old_node:
                for oc in old_node.children:
                    if oc is not new_node and not oc.concept.startswith("__OTHER__"):
                        new_node.add_child(oc)
                root.children[i] = new_node
                return True
            if self._replace_in_tree(child, old_node, new_node, _visited):
                return True
        return False
