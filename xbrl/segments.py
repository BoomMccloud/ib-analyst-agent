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

    biz_dim = "us-gaap:StatementBusinessSegmentsAxis"
    prod_dim = "srt:ProductOrServiceAxis"

    biz_members = None
    biz_tag = None
    for tag in rev_tags:
        tag_segs = seg_facts.get(tag, {})
        members = tag_segs.get(biz_dim)
        if not members:
            continue
        member_sum = {}
        for m_vals in members.values():
            for p, v in m_vals.items():
                if p in period_set:
                    member_sum[p] = member_sum.get(p, 0) + v
        all_close = all(
            abs(member_sum.get(p, 0) - total_values.get(p, 0)) / max(abs(total_values[p]), 1) < 0.01
            for p in periods if total_values.get(p, 0) != 0
        )
        if all_close and len(members) >= 2:
            biz_members = members
            biz_tag = tag
            break

    if not biz_members:
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

        root = TreeNode("_REVENUE_SEGMENTS", weight=1.0)
        root.name = "Revenue Segments"
        root.values = dict(total_values)
        root.is_leaf = False
        for member, vals in sorted(prod_members.items(), key=lambda x: -sum(abs(v) for v in x[1].values())):
            child = TreeNode(member.replace(':', '_', 1), weight=1.0)
            child.name = get_label(member, lab_labels)
            child.values = {p: v for p, v in vals.items() if p in period_set}
            child.is_leaf = True
            root.add_child(child)
        print(f"  Revenue segments: {len(root.children)} products (ProductOrServiceAxis)", file=sys.stderr)
        return root

    root = TreeNode("_REVENUE_SEGMENTS", weight=1.0)
    root.name = "Revenue Segments"
    root.values = dict(total_values)
    root.is_leaf = False

    prod_biz_dims = tuple(sorted([prod_dim, biz_dim]))
    multi_2d = {}
    for tag in rev_tags:
        tag_multi = multi_seg_facts.get(tag, {})
        if prod_biz_dims in tag_multi:
            multi_2d = tag_multi[prod_biz_dims]
            break

    dim_order = list(prod_biz_dims)
    prod_idx = dim_order.index(prod_dim)
    biz_idx = dim_order.index(biz_dim)

    seg_sum = {p: 0.0 for p in periods}
    for seg_member, seg_vals in sorted(biz_members.items(), key=lambda x: -sum(abs(v) for v in x[1].values())):
        seg_node = TreeNode(seg_member.replace(':', '_', 1), weight=1.0)
        seg_node.name = get_label(seg_member, lab_labels)
        seg_node.values = {p: v for p, v in seg_vals.items() if p in period_set}

        for p in periods:
            seg_sum[p] += seg_node.values.get(p, 0)

        inner_members = {}
        for member_tuple, vals in multi_2d.items():
            if member_tuple[biz_idx] == seg_member:
                prod_member = member_tuple[prod_idx]
                inner_members[prod_member] = {p: v for p, v in vals.items() if p in period_set}

        if inner_members:
            leaves = _find_best_decomposition(inner_members, seg_node.values, periods)
            if leaves:
                for leaf_member in leaves:
                    child = TreeNode(leaf_member.replace(':', '_', 1), weight=1.0)
                    child.name = get_label(leaf_member, lab_labels)
                    child.values = dict(inner_members[leaf_member])
                    child.is_leaf = True
                    seg_node.add_child(child)
                print(f"  Revenue segments: {seg_node.name} → {len(leaves)} products", file=sys.stderr)
            else:
                seg_node.is_leaf = True
        else:
            seg_node.is_leaf = True
        root.add_child(seg_node)

    for p in periods:
        gap = total_values.get(p, 0) - seg_sum.get(p, 0)
        if abs(gap) > 0.5:
            elim_node = TreeNode("_ELIMINATIONS", weight=1.0)
            elim_node.name = "Hedging & Eliminations"
            elim_node.values = {p: total_values.get(p, 0) - seg_sum.get(p, 0) for p in periods}
            elim_node.is_leaf = True
            root.add_child(elim_node)
            print(f"  Revenue segments: added Hedging & Eliminations", file=sys.stderr)
            break

    if len(root.children) >= 2:
        return root
    return None
