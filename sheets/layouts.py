def _cascade_layout(node, current_row, indent=0):
    layout = []
    if not node.children:
        return [(current_row, indent, node)]
    
    backbone = None
    expense_children = []
    for child in node.children:
        if getattr(child, 'weight', 1.0) == 1.0 and child.children and backbone is None:
            backbone = child
        else:
            expense_children.append(child)
            
    if backbone:
        backbone_rows = _cascade_layout(backbone, current_row, indent)
        layout.extend(backbone_rows)
        current_row = backbone_rows[-1][0] + 1
        
        for child in expense_children:
            if child.children:
                def _assign_layout(n, ind):
                    nonlocal current_row
                    res = [(current_row, ind, n)]
                    current_row += 1
                    for c in n.children:
                        res.extend(_assign_layout(c, ind + 1))
                    return res
                sub_rows = _assign_layout(child, indent + 1)
            else:
                sub_rows = [(current_row, indent + 1, child)]
                current_row += 1
            layout.extend(sub_rows)
    else:
        plus_children = [c for c in node.children if getattr(c, 'weight', 1.0) == 1.0]
        minus_children = [c for c in node.children if getattr(c, 'weight', 1.0) != 1.0]
        for child in plus_children + minus_children:
            if child.children:
                def _assign_layout(n, ind):
                    nonlocal current_row
                    res = [(current_row, ind, n)]
                    current_row += 1
                    for c in n.children:
                        res.extend(_assign_layout(c, ind + 1))
                    return res
                sub_rows = _assign_layout(child, indent + 1)
            else:
                sub_rows = [(current_row, indent + 1, child)]
                current_row += 1
            layout.extend(sub_rows)
            
    layout.append((current_row, indent, node))
    return layout

def _totals_at_bottom_layout(node, current_row, indent=0):
    """Post-order layout: children first, parent (total) at bottom."""
    layout = []
    if not node.children:
        return [(current_row, indent, node)]
    for child in node.children:
        child_rows = _totals_at_bottom_layout(child, current_row, indent + 1)
        layout.extend(child_rows)
        current_row = child_rows[-1][0] + 1
    layout.append((current_row, indent, node))
    return layout
