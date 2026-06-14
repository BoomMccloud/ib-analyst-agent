import sys
from .tree import TreeNode, find_node_by_role
from .linkbase import get_label

_SEGMENT_DIM_PRIORITY = [
    "srt:ProductOrServiceAxis",
    "us-gaap:StatementBusinessSegmentsAxis",
]

def _find_best_decomposition(members_data: dict, total_values: dict, periods: list[str]) -> list[str] | None:
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

    for size in range(len(member_names), 1, -1):
        for subset in combinations(member_names, size):
            if subset_sums_to_total(subset):
                return list(subset)
    return None

def _detect_segments_for_node(node: TreeNode, seg_facts: dict, periods: list[str]) -> tuple[list[str], dict] | None:
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

def _attach_segment_children(node: TreeNode, leaf_members: list[str], member_values: dict, lab_labels: dict, periods: list[str]):
    period_set = set(periods)
    for member in leaf_members:
        child = TreeNode(member.replace(':', '_', 1), weight=1.0)
        child.name = get_label(member, lab_labels)
        child.values = {p: v for p, v in member_values[member].items() if p in period_set}
        child.is_leaf = True
        child.role = None
        node.add_child(child)

def _attach_is_segments(trees: dict, seg_facts: dict, lab_labels: dict):
    is_tree = trees.get("IS")
    if not is_tree:
        return
    periods = trees.get("complete_periods", [])
    if not periods:
        return

    def _collect_segment_targets(node):
        if not node:
            return []
        if node.is_leaf:
            return [node]
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
                        print(f"  Segments: shared {dim} — Revenue ({len(rev_leaves)} segments), COGS ({len(cogs_leaves)} segments)", file=sys.stderr)
                        _attach_segment_children(rt, rev_leaves, {m: rev_members[m] for m in rev_leaves}, lab_labels, periods)
                        _attach_segment_children(ct, cogs_leaves, {m: cogs_members[m] for m in cogs_leaves}, lab_labels, periods)
                        shared_done.update([id(rt), id(ct)])
            if shared_done:
                break
    targets = [t for t in targets if id(t) not in shared_done]
    for node in targets:
        result = _detect_segments_for_node(node, seg_facts, periods)
        if result:
            leaf_members, member_values = result
            role_label = "Revenue" if node.role == "IS_REVENUE" else "COGS"
            print(f"  Segments: {role_label} → {len(leaf_members)} segments", file=sys.stderr)
            _attach_segment_children(node, leaf_members, member_values, lab_labels, periods)

def find_decomposition_with_gap(members_data: dict, total_values: dict, periods: list[str]) -> tuple[list[str], dict | None]:
    """Finds best subset of members that decomposes the total.
    First tries to find an exact sum (within 0.5).
    If that fails, tries to use all active members, and if their sum covers
    between 80% and 105% of total, returns them with gap-absorption values.
    """
    # 1. Try exact sum
    exact_leaves = _find_best_decomposition(members_data, total_values, periods)
    if exact_leaves:
        return exact_leaves, None
        
    # 2. Relaxed sum (use all members with values)
    member_names = list(members_data.keys())
    if not member_names or not total_values:
        return [], None
        
    active_members = [m for m in member_names if any(members_data[m].get(p, 0) != 0 for p in periods)]
    if len(active_members) < 2:
        return [], None
        
    valid_relaxed = True
    gap_values = {}
    for p in periods:
        t = total_values.get(p, 0)
        if t == 0:
            continue
        s = sum(members_data[m].get(p, 0) for m in active_members)
        ratio = s / t
        if not (0.80 <= ratio <= 1.05):
            valid_relaxed = False
            break
        gap_values[p] = t - s
        
    if valid_relaxed:
        return active_members, gap_values
        
    return [], None

def choose_best_candidate_programmatic(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    # Priority:
    # 1. Nested
    # 2. ProductOrServiceAxis
    # 3. StatementBusinessSegmentsAxis
    # 4. StatementGeographicalAxis
    # 5. Any other candidate
    for c in candidates:
        if c["type"] == "nested":
            return c
    for c in candidates:
        if c.get("dim") == "srt:ProductOrServiceAxis":
            return c
    for c in candidates:
        if c.get("dim") == "us-gaap:StatementBusinessSegmentsAxis":
            return c
    for c in candidates:
        if c.get("dim") == "srt:StatementGeographicalAxis":
            return c
    return candidates[0]

def _build_revenue_segment_tree(trees: dict, seg_facts: dict, multi_seg_facts: dict, lab_labels: dict) -> TreeNode | None:
    is_tree = trees.get("IS")
    if not is_tree:
        return None
    periods = trees.get("complete_periods", [])
    if not periods:
        return None
    period_set = set(periods)
    rev_node = find_node_by_role(is_tree, "IS_REVENUE")
    if not rev_node:
        return None

    rev_tags = set()
    def _collect_tags(node):
        rev_tags.add(node.tag)
        for child in node.children:
            _collect_tags(child)
    _collect_tags(rev_node)
    rev_tags.update([
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:SalesRevenueNet",
    ])

    total_values = {p: v for p, v in rev_node.values.items() if p in period_set}
    if not total_values:
        return None

    target_dims = [
        "srt:ProductOrServiceAxis",
        "us-gaap:StatementBusinessSegmentsAxis",
        "srt:StatementGeographicalAxis",
    ]

    candidates = []

    # 1. Scan single-axis decompositions
    for tag in rev_tags:
        tag_segs = seg_facts.get(tag, {})
        for dim in target_dims:
            if dim in tag_segs:
                members = tag_segs[dim]
                leaves, gap_values = find_decomposition_with_gap(members, total_values, periods)
                if leaves and len(leaves) >= 2:
                    candidates.append({
                        "type": "single",
                        "tag": tag,
                        "dim": dim,
                        "id": f"{tag} | {dim}",
                        "leaves": leaves,
                        "gap_values": gap_values
                    })

    # 2. Scan multi-dimensional nested decompositions
    for tag in rev_tags:
        tag_multi = multi_seg_facts.get(tag, {})
        for dim_tuple, member_mappings in tag_multi.items():
            if len(dim_tuple) == 2:
                dim0, dim1 = dim_tuple
                if dim0 not in target_dims or dim1 not in target_dims:
                    continue
                level1_members = seg_facts.get(tag, {}).get(dim0, {})
                if not level1_members:
                    continue
                level1_leaves, l1_gap_values = find_decomposition_with_gap(level1_members, total_values, periods)
                if level1_leaves:
                    nested_leaves = {}
                    nested_gap_values = {}
                    nested_member_values = {}
                    for l1_m in level1_leaves:
                        l2_members = {}
                        for member_tuple, vals in member_mappings.items():
                            if l1_m in member_tuple:
                                l2_m = member_tuple[1] if member_tuple[0] == l1_m else member_tuple[0]
                                l2_members[l2_m] = vals
                        
                        l1_m_values = level1_members[l1_m]
                        l2_leaves, l2_gap_values = find_decomposition_with_gap(l2_members, l1_m_values, periods)
                        if l2_leaves:
                            nested_leaves[l1_m] = l2_leaves
                            nested_gap_values[l1_m] = l2_gap_values
                            nested_member_values[l1_m] = {m: l2_members[m] for m in l2_leaves}
                        else:
                            nested_leaves[l1_m] = []
                            
                    candidates.append({
                        "type": "nested",
                        "tag": tag,
                        "dim0": dim0,
                        "dim1": dim1,
                        "id": f"nested_{tag}_{dim0}_by_{dim1}",
                        "level1_leaves": level1_leaves,
                        "nested_leaves": nested_leaves,
                        "nested_gap_values": nested_gap_values,
                        "nested_member_values": nested_member_values,
                        "l1_gap_values": l1_gap_values,
                        "level1_members": level1_members
                    })

    best = choose_best_candidate_programmatic(candidates)
    if not best:
        return None

    root = TreeNode("_REVENUE_SEGMENTS", weight=1.0)
    root.name = "Revenue Segments"
    root.values = dict(total_values)
    root.is_leaf = False

    if best["type"] == "single":
        tag = best["tag"]
        dim = best["dim"]
        leaves = best["leaves"]
        gap_values = best["gap_values"]
        members = seg_facts[tag][dim]
        
        for member in sorted(leaves, key=lambda m: -sum(abs(v) for v in members[m].values())):
            child = TreeNode(member.replace(':', '_', 1), weight=1.0)
            child.name = get_label(member, lab_labels)
            child.values = {p: v for p, v in members[member].items() if p in period_set}
            child.is_leaf = True
            root.add_child(child)
            
        if gap_values:
            gap_node = TreeNode("_REVENUE_SEGMENTS_RESIDUAL", weight=1.0)
            gap_node.name = "Corporate & Other (Residual)"
            gap_node.values = {p: v for p, v in gap_values.items() if p in period_set}
            gap_node.is_leaf = True
            root.add_child(gap_node)
            
        print(f"  Revenue segments (Pipeline): selected single axis {dim} (Tag: {tag}) with {len(leaves)} segments" + (" and residual" if gap_values else ""), file=sys.stderr)
        
    else:  # nested
        tag = best["tag"]
        dim0 = best["dim0"]
        dim1 = best["dim1"]
        level1_leaves = best["level1_leaves"]
        nested_leaves = best["nested_leaves"]
        nested_gap_values = best["nested_gap_values"]
        nested_member_values = best["nested_member_values"]
        l1_gap_values = best["l1_gap_values"]
        level1_members = best["level1_members"]
        
        for l1_m in sorted(level1_leaves, key=lambda m: -sum(abs(v) for v in level1_members[m].values())):
            l1_node = TreeNode(l1_m.replace(':', '_', 1), weight=1.0)
            l1_node.name = get_label(l1_m, lab_labels)
            l1_node.values = {p: v for p, v in level1_members[l1_m].items() if p in period_set}
            
            l2_leaves = nested_leaves[l1_m]
            if l2_leaves:
                l1_node.is_leaf = False
                l2_vals = nested_member_values[l1_m]
                for l2_m in sorted(l2_leaves, key=lambda m: -sum(abs(v) for v in l2_vals[m].values())):
                    child = TreeNode(l2_m.replace(':', '_', 1), weight=1.0)
                    child.name = get_label(l2_m, lab_labels)
                    child.values = {p: v for p, v in l2_vals[l2_m].items() if p in period_set}
                    child.is_leaf = True
                    l1_node.add_child(child)
                
                l2_gap = nested_gap_values[l1_m]
                if l2_gap:
                    gap_node = TreeNode(f"{l1_m.replace(':', '_', 1)}_RESIDUAL", weight=1.0)
                    gap_node.name = "Corporate & Other (Residual)"
                    gap_node.values = {p: v for p, v in l2_gap.items() if p in period_set}
                    gap_node.is_leaf = True
                    l1_node.add_child(gap_node)
            else:
                l1_node.is_leaf = True
                
            root.add_child(l1_node)
            
        if l1_gap_values:
            gap_node = TreeNode("_REVENUE_SEGMENTS_RESIDUAL", weight=1.0)
            gap_node.name = "Corporate & Other (Residual)"
            gap_node.values = {p: v for p, v in l1_gap_values.items() if p in period_set}
            gap_node.is_leaf = True
            root.add_child(gap_node)
            
        print(f"  Revenue segments (Pipeline): selected 2-level nested axes {dim0} by {dim1} (Tag: {tag}) with {len(level1_leaves)} L1 segments", file=sys.stderr)

    if len(root.children) >= 2:
        return root
    return None
