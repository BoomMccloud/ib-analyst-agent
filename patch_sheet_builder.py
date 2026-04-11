import re

with open('sheet_builder.py', 'r') as f:
    content = f.read()

cascade_func = """def _cascade_layout(node, current_row, indent=0):
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
"""

render_old = """def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name):
    \"\"\"Render a tree into rows. Leaves get values, parents get formulas.\"\"\"
    layout = []
    def _assign_rows(node, indent=0):
        row_num = start_row + len(layout)
        layout.append((row_num, indent, node))
        for child in node.children:
            _assign_rows(child, indent + 1)
    
    _assign_rows(tree)"""

render_new = cascade_func + """
def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name, is_cascade=False):
    \"\"\"Render a tree into rows. Leaves get values, parents get formulas.\"\"\"
    if is_cascade:
        layout = _cascade_layout(tree, start_row, 0)
    else:
        layout = []
        def _assign_rows(node, indent=0):
            row_num = start_row + len(layout)
            layout.append((row_num, indent, node))
            for child in node.children:
                _assign_rows(child, indent + 1)
        _assign_rows(tree)"""

content = content.replace(render_old, render_new)

# Update write_sheets to use is_cascade=True for IS
is_call_old = """body_rows = _render_sheet_body(is_tree, periods, start_row=len(header_rows)+1, global_role_map=global_role_map, sheet_name="IS")"""
is_call_new = """body_rows = _render_sheet_body(is_tree, periods, start_row=len(header_rows)+1, global_role_map=global_role_map, sheet_name="IS", is_cascade=True)"""

content = content.replace(is_call_old, is_call_new)

with open('sheet_builder.py', 'w') as f:
    f.write(content)
