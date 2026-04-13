from xbrl_tree import CROSS_STATEMENT_CHECKS, TreeNode, find_node_by_role
from gws_utils import gws_write
from sheets.formulas import dcol, _build_weight_formula, _cell_ref
from sheets.layouts import _cascade_layout, _totals_at_bottom_layout

def _render_cross_checks(checks, role_map: dict, periods: list[str]) -> list:
    """Render cross-statement check rows. Skips checks with missing roles."""
    rows = []
    for check in (checks if isinstance(checks, (list, tuple)) else list(checks.values())):
        roles = check.get("roles", [])
        if not all(r in role_map for r in roles):
            continue
        row = ["", "", check.get("name", "Check"), ""]
        for i in range(len(periods)):
            col = dcol(i)
            refs = {}
            for role in roles:
                sheet_name, row_num = role_map[role]
                refs[role] = f"'{sheet_name}'!{col}{row_num}"
            if len(roles) == 2:
                refs["left"] = refs[roles[0]]
                refs["right"] = refs[roles[1]]
            try:
                formula = check["formula"].format(**refs)
            except KeyError:
                formula = ""
            row.append(formula)
        rows.append(row)
    return rows

def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name, is_cascade=False, totals_at_bottom=False):
    """Render a tree into rows. Leaves get values, parents get formulas.
    Returns (rows, row_types) where row_types is a parallel list of
    "parent", "leaf", etc. for formatting.
    """
    if is_cascade:
        layout = _cascade_layout(tree, start_row, 0)
    elif totals_at_bottom:
        layout = _totals_at_bottom_layout(tree, start_row, 0)
    else:
        layout = []
        def _assign_rows(node, indent=0):
            row_num = start_row + len(layout)
            layout.append((row_num, indent, node))
            for child in node.children:
                _assign_rows(child, indent + 1)
        _assign_rows(tree)
    node_row = {id(entry[2]): entry[0] for entry in layout}

    rows = []
    row_types = []
    for row_num, indent, node in layout:
        label = ("  " * indent) + node.name
        if node.role:
            global_role_map[node.role] = (sheet_name, row_num)

        row = ["", "", label, ""]
        if not node.children:
            for p in periods:
                val = node.values.get(p, 0)
                row.append(round(val) if val else "")
            row_types.append("leaf")
        else:
            child_rows = [(node_row[id(c)], c.weight) for c in node.children]
            for i in range(len(periods)):
                col = dcol(i)
                row.append(_build_weight_formula(col, child_rows))
            row_types.append("parent")
        rows.append(row)
    return rows, row_types

def _render_cf_with_separators(cf_tree, periods, start_row, global_role_map):
    """Render CF tree with blank rows after each major section total
    (Operating, Investing, Financing)."""
    rows = []
    row_types = []
    current_start = start_row

    # Render each section (child of root) separately, with a blank after each
    section_roles = {"CF_OPCF", "CF_INVCF", "CF_FINCF"}
    for child in cf_tree.children:
        section_rows, section_types = _render_sheet_body(
            child, periods, start_row=current_start,
            global_role_map=global_role_map, sheet_name="CF",
            totals_at_bottom=True)
        rows.extend(section_rows)
        row_types.extend(section_types)
        current_start += len(section_rows)

        if child.role in section_roles:
            rows.append([""] * (4 + len(periods)))
            row_types.append("blank")
            current_start += 1

    # Render the root total (Net Change in Cash) at the end
    root_row_num = current_start
    if cf_tree.role:
        global_role_map[cf_tree.role] = ("CF", root_row_num)
    # Build child references from all section totals already rendered
    node_row = {}
    for idx, (row, rtype) in enumerate(zip(rows, row_types)):
        node_row[idx] = start_row + idx

    # Find the row numbers of each direct child (section totals)
    child_row_nums = []
    search_row = start_row
    for child in cf_tree.children:
        # Each section was rendered with totals_at_bottom, so the section total
        # is the last non-blank row of that section
        section_rows, _ = _render_sheet_body(
            child, periods, start_row=0,
            global_role_map={}, sheet_name="CF",
            totals_at_bottom=True)
        section_len = len(section_rows)
        total_row = search_row + section_len - 1
        child_row_nums.append((total_row, child.weight))
        search_row += section_len
        if child.role in section_roles:
            search_row += 1  # blank row

    root_row = ["", "", cf_tree.name, ""]
    for i in range(len(periods)):
        col = dcol(i)
        root_row.append(_build_weight_formula(col, child_row_nums))
    rows.append(root_row)
    row_types.append("parent")

    return rows, row_types

def _write_sheet_tab(sid, sheet_name, rows, periods, tree, global_role_map):
    gws_write(sid, f"{sheet_name}!A1:{dcol(len(periods)-1)}{len(rows)}", rows)

def _add_check_row(rows, periods, formula_fn):
    row = ["", "", "Check", ""]
    for i in range(len(periods)):
        col = dcol(i)
        f = formula_fn(col)
        row.append(f if f else "")
    rows.append(row)

def _write_summary_tab(sid, periods, global_role_map) -> list:
    rows = [
        [],
        ["", "", "3-Statement Summary", ""] + list(periods),
        [],
    ]
    def _add_summary_row(label, role, target_role):
        row_num = len(rows) + 1
        row = ["", "", label, ""]
        for i in range(len(periods)):
            col = dcol(i)
            row.append(f"={_cell_ref(target_role, col, global_role_map)}")
        rows.append(row)
        global_role_map[role] = ("Summary", row_num)

    _add_summary_row("Total Assets", "SUMM_TA", "BS_TA")
    _add_summary_row("Total Liabilities", "SUMM_TL", "BS_TL")
    _add_summary_row("Total L&E", "SUMM_TLE", "BS_TLE")
    _add_summary_row("Operating Cash Flow", "SUMM_OPCF", "CF_OPCF")
    _add_summary_row("Beginning Cash", "SUMM_BEGC", "CF_BEGC")
    _add_summary_row("Net Change in Cash", "SUMM_NETCH", "CF_NETCH")
    _add_summary_row("Ending Cash", "SUMM_ENDC", "CF_ENDC")

    # --- Declarative cross-statement checks (replaces tautological closures) ---
    rows.append([""] * (4 + len(periods)))  # blank separator
    check_rows = _render_cross_checks(CROSS_STATEMENT_CHECKS, global_role_map, periods)
    rows.extend(check_rows)

    gws_write(sid, f"Summary!A1:{dcol(len(periods)-1)}{len(rows)}", rows)
    return rows

def _render_revenue_segments(seg_tree, periods, start_row):
    """Render revenue segment tree into rows for the IS tab.

    Returns (rows, row_types, total_row_num) where total_row_num is the
    1-based sheet row of the "Total Revenue" summation line.
    """
    rows = []
    row_types = []
    row_map = {}  # id(node) -> row_num

    def _walk(node, indent=0):
        row_num = start_row + len(rows)
        row_map[id(node)] = row_num
        label = ("  " * indent) + node.name

        row = ["", "", label, ""]
        if not node.children:
            # Leaf: hardcoded values
            for p in periods:
                val = node.values.get(p, 0)
                row.append(round(val) if val else "")
            rows.append(row)
            row_types.append("leaf")
        else:
            # First render children
            child_start = len(rows) + 1  # placeholder, we'll add parent after children
            rows.append(None)  # placeholder
            row_types.append(None)
            for child in node.children:
                _walk(child, indent + 1)
            # Now fill in the parent row with SUM formula
            child_rows = [(row_map[id(c)], c.weight) for c in node.children]
            for i in range(len(periods)):
                col = dcol(i)
                row.append(_build_weight_formula(col, child_rows))
            idx = row_num - start_row
            rows[idx] = row
            row_types[idx] = "parent"

    # Don't render the root node name ("Revenue Segments") — start with its children
    # But we do want a total line at the end
    for child in seg_tree.children:
        _walk(child, indent=0)

    # Add "Total Revenue" summation row
    total_row_num = start_row + len(rows)
    total_row = ["", "", "Total Revenue", ""]
    child_rows = [(row_map[id(c)], c.weight) for c in seg_tree.children]
    for i in range(len(periods)):
        col = dcol(i)
        total_row.append(_build_weight_formula(col, child_rows))
    rows.append(total_row)
    row_types.append("parent")

    return rows, row_types, total_row_num

def _link_revenue_to_segments(body_rows, body_types, is_tree, periods,
                               seg_total_row_num, is_start_row):
    """Replace the IS revenue row's values/formula with a reference to the
    segment total row, so it pulls from the segments above."""
    if isinstance(is_tree, dict):
        is_tree = TreeNode.from_dict(is_tree)
    rev_node = find_node_by_role(is_tree, "IS_REVENUE")
    if not rev_node:
        return

    # Find which body row corresponds to the revenue node
    # The body rows start at is_start_row; search for the revenue label
    for idx, row in enumerate(body_rows):
        if len(row) < 3:
            continue
        label = row[2].strip() if isinstance(row[2], str) else ""
        # Match by name — strip indentation
        if label == rev_node.name:
            # Replace data cells with reference to segment total
            for i in range(len(periods)):
                col = dcol(i)
                row[4 + i] = f"={col}{seg_total_row_num}"
            break
