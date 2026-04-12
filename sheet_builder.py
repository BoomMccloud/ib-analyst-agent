import argparse
import json
import sys

from gws_utils import _run_gws, gws_write, gws_batch_update

def gws_create(title, sheet_names):
    sheets = [{"properties": {"title": s}} for s in sheet_names]
    r = _run_gws("sheets", "spreadsheets", "create", "--json",
                  json.dumps({"properties": {"title": title}, "sheets": sheets}))
    sid = r["spreadsheetId"]
    url = r["spreadsheetUrl"]
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in r["sheets"]}
    return sid, url, sheet_ids

def dcol(i):
    """Data column letter(s). i=0 → E, i=1 → F, etc. (data starts col E)."""
    col_num = i + 4
    result = ""
    while col_num >= 0:
        result = chr(65 + col_num % 26) + result
        col_num = col_num // 26 - 1
    return result

def _build_weight_formula(col: str, child_rows: list[tuple[int, float]]) -> str:
    """Build a cell formula from child row numbers and XBRL weights."""
    if not child_rows:
        return ""
    if len(child_rows) == 1:
        r, w = child_rows[0]
        return f"={col}{r}" if w == 1.0 else f"=-{col}{r}"
    all_positive = all(w == 1.0 for _, w in child_rows)
    if all_positive:
        row_nums = [r for r, _ in child_rows]
        if row_nums == list(range(row_nums[0], row_nums[-1] + 1)):
            return f"=SUM({col}{row_nums[0]}:{col}{row_nums[-1]})"
        else:
            return "=" + "+".join(f"{col}{r}" for r, _ in child_rows)
    parts = []
    for r, w in child_rows:
        sign = "+" if w == 1.0 else "-"
        parts.append(f"{sign}{col}{r}")
    return "=" + "".join(parts).lstrip("+")

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

def prev_period(p: str, periods: list[str]) -> str | None:
    idx = periods.index(p)
    return periods[idx - 1] if idx > 0 else None

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

def _write_sheet_tab(sid, sheet_name, rows, periods, tree, global_role_map):
    gws_write(sid, f"{sheet_name}!A1:{dcol(len(periods)-1)}{len(rows)}", rows)

def _cell_ref(role, col, global_role_map):
    entry = global_role_map.get(role)
    if not entry:
        print(f"WARNING: Role {role} not found in global_role_map", file=sys.stderr)
        return "0"
    sheet_name, row_num = entry
    return f"'{sheet_name}'!{col}{row_num}"

def _add_check_row(rows, periods, formula_fn):
    row = ["", "", "Check", ""]
    for i in range(len(periods)):
        col = dcol(i)
        f = formula_fn(col)
        row.append(f if f else "")
    rows.append(row)

def _write_summary_tab(sid, periods, global_role_map) -> list:
    from xbrl_tree import CROSS_STATEMENT_CHECKS

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

def _build_format_requests(sheet_id, rows, periods, row_types=None, unit_label="$m"):
    """Build Google Sheets formatting requests matching Wall Street/IB style.

    Formatting rules (from template):
    - Header row: SOLID_MEDIUM bottom border, right-aligned years
    - Parent/subtotal rows: "$"#,##0 currency format + SOLID top border
    - Leaf rows: #,##0 plain number format
    - Check rows: 0;(0);- italic
    - Font size 10 throughout, no bold
    """
    requests = []
    num_data_cols = len(periods)
    data_start_col = 4  # col E
    end_col = data_start_col + num_data_cols
    if row_types is None:
        row_types = []

    # Solid border style helpers
    SOLID = {"style": "SOLID", "color": {"red": 0, "green": 0, "blue": 0}}
    SOLID_MEDIUM = {"style": "SOLID_MEDIUM", "color": {"red": 0, "green": 0, "blue": 0}}
    # IB convention: blue = hardcoded input, black = formula
    BLUE = {"red": 0, "green": 0, "blue": 1}
    BLACK = {"red": 0, "green": 0, "blue": 0}

    for row_idx, row in enumerate(rows):
        if len(row) < 3:
            continue
        label = row[2].strip() if isinstance(row[2], str) else ""
        rtype = row_types[row_idx] if row_idx < len(row_types) else None

        # --- Header row ($m + years): SOLID_MEDIUM bottom border ---
        if label == unit_label or label == "3-Statement Summary":
            requests.append({
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2, "endColumnIndex": end_col
                    },
                    "bottom": SOLID_MEDIUM
                }
            })
            # Right-align year headers in data columns
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {
                        "horizontalAlignment": "RIGHT",
                        "textFormat": {"fontSize": 10}
                    }},
                    "fields": "userEnteredFormat(horizontalAlignment,textFormat.fontSize)"
                }
            })

        # --- Check rows: italic, 0;(0);- format ---
        elif label == "Check":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 0, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"italic": True, "fontSize": 10}}},
                    "fields": "userEnteredFormat.textFormat"
                }
            })
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0;(0);-"}}},
                    "fields": "userEnteredFormat.numberFormat"
                }
            })

        # --- Parent/subtotal rows: "$"#,##0 + SOLID top border + black text ---
        elif rtype == "parent":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": end_col
                    },
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0'},
                        "horizontalAlignment": "RIGHT",
                        "textFormat": {"fontSize": 10, "foregroundColorStyle": {"rgbColor": BLACK}}
                    }},
                    "fields": "userEnteredFormat(numberFormat,horizontalAlignment,textFormat)"
                }
            })
            requests.append({
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2, "endColumnIndex": end_col
                    },
                    "top": SOLID
                }
            })

        # --- Leaf rows: plain #,##0 + blue text (hardcoded values) ---
        elif rtype == "leaf" or (label and label not in (unit_label, "3-Statement Summary", "Check")):
            has_data = any(isinstance(cell, (int, float)) or (isinstance(cell, str) and cell.startswith("=")) for cell in row[4:])
            if has_data:
                # Determine color: blue for hardcoded values, black for formulas
                has_formula = any(isinstance(cell, str) and cell.startswith("=") for cell in row[4:])
                color = BLACK if has_formula else BLUE
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                            "startColumnIndex": data_start_col, "endColumnIndex": end_col
                        },
                        "cell": {"userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                            "horizontalAlignment": "RIGHT",
                            "textFormat": {"fontSize": 10, "foregroundColorStyle": {"rgbColor": color}}
                        }},
                        "fields": "userEnteredFormat(numberFormat,horizontalAlignment,textFormat)"
                    }
                })

    # --- Global: set font size 10 and left-align label column for all rows ---
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": len(rows),
                "startColumnIndex": 2, "endColumnIndex": 3
            },
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "LEFT",
                "textFormat": {"fontSize": 10}
            }},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat.fontSize)"
        }
    })

    return requests


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
    from xbrl_tree import TreeNode, find_node_by_role
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


def write_sheets(trees: dict, company: str) -> tuple[str, str]:
    periods = trees.get("complete_periods", [])
    unit_label = trees.get("unit_label", "$m")
    sid, url, sheet_ids = gws_create(f"{company} — Financial Model", ["IS", "BS", "CF", "Summary"])
    global_role_map = {}
    tab_rows = {}       # sheet_name -> list of row lists
    tab_row_types = {}  # sheet_name -> list of row type strings

    # --- IS tab ---
    is_tree = trees.get("IS")
    if is_tree:
        header_rows = [[], ["", "", unit_label, ""] + list(periods), []]
        header_types = ["blank", "header", "blank"]

        # Revenue segments section (above the IS body)
        rev_seg = trees.get("revenue_segments")
        seg_rows = []
        seg_types = []
        seg_total_row_num = None
        if rev_seg:
            from xbrl_tree import TreeNode
            if isinstance(rev_seg, dict):
                rev_seg = TreeNode.from_dict(rev_seg)
            seg_rows, seg_types, seg_total_row_num = _render_revenue_segments(
                rev_seg, periods, start_row=len(header_rows) + 1)
            # Add blank separator after segments
            seg_rows.append([""] * (4 + len(periods)))
            seg_types.append("blank")

        is_start_row = len(header_rows) + len(seg_rows) + 1
        body_rows, body_types = _render_sheet_body(
            is_tree, periods, start_row=is_start_row,
            global_role_map=global_role_map, sheet_name="IS", is_cascade=True)

        # If we have segments, make the IS revenue row reference the segment total
        if seg_total_row_num is not None:
            _link_revenue_to_segments(body_rows, body_types, is_tree, periods,
                                      seg_total_row_num, is_start_row)

        is_rows = header_rows + seg_rows + body_rows
        is_types = header_types + seg_types + body_types
        tab_rows["IS"] = is_rows
        tab_row_types["IS"] = is_types
        _write_sheet_tab(sid, "IS", is_rows, periods, is_tree, global_role_map)

    # --- BS tab ---
    bs_tree = trees.get("BS")
    bs_le_tree = trees.get("BS_LE")
    if bs_tree or bs_le_tree:
        header_rows = [[], ["", "", unit_label, ""] + list(periods), []]
        header_types = ["blank", "header", "blank"]
        body_rows = []
        body_types = []
        current_row = len(header_rows) + 1
        if bs_tree:
            assets_rows, assets_types = _render_sheet_body(bs_tree, periods, start_row=current_row, global_role_map=global_role_map, sheet_name="BS", totals_at_bottom=True)
            body_rows += assets_rows
            body_types += assets_types
            current_row += len(assets_rows)
            body_rows.append([""] * (4 + len(periods)))
            body_types.append("blank")
            current_row += 1
        if bs_le_tree:
            le_rows, le_types = _render_sheet_body(bs_le_tree, periods, start_row=current_row, global_role_map=global_role_map, sheet_name="BS", totals_at_bottom=True)
            body_rows += le_rows
            body_types += le_types
        bs_rows = header_rows + body_rows
        bs_types = header_types + body_types
        tab_rows["BS"] = bs_rows
        tab_row_types["BS"] = bs_types
        _write_sheet_tab(sid, "BS", bs_rows, periods, None, global_role_map)

    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", unit_label, ""] + list(periods), []]
        header_types = ["blank", "header", "blank"]
        body_rows, body_types = _render_cf_with_separators(
            cf_tree, periods, start_row=len(header_rows)+1,
            global_role_map=global_role_map)

        current_row = len(header_rows) + len(body_rows) + 1
        body_rows.append([""] * (4 + len(periods)))
        body_types.append("blank")
        current_row += 1

        # --- Cash Proof ---
        cf_endc_values = trees.get("cf_endc_values", {})
        sorted_endc_dates = sorted(cf_endc_values.keys())

        begc_row_num = current_row
        begc_row = ["", "", "Beginning Cash", ""]
        for p in periods:
            prev_dates = [d for d in sorted_endc_dates if d < p]
            begc_row.append(round(cf_endc_values[prev_dates[-1]]) if prev_dates else "")
        body_rows.append(begc_row)
        body_types.append("leaf")
        global_role_map["CF_BEGC"] = ("CF", begc_row_num)
        current_row += 1

        netch_row_num = current_row
        netch_ref = global_role_map.get("CF_NETCH")
        netch_row = ["", "", "Net Change in Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            netch_row.append(f"={col}{netch_ref[1]}" if netch_ref else "")
        body_rows.append(netch_row)
        body_types.append("leaf")
        current_row += 1

        endc_row_num = current_row
        endc_row = ["", "", "Ending Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            endc_row.append(f"={col}{begc_row_num}+{col}{netch_row_num}")
        body_rows.append(endc_row)
        body_types.append("parent")  # Ending Cash is a computed total
        global_role_map["CF_ENDC"] = ("CF", endc_row_num)

        cf_rows = header_rows + body_rows
        cf_types = header_types + body_types
        tab_rows["CF"] = cf_rows
        tab_row_types["CF"] = cf_types
        _write_sheet_tab(sid, "CF", cf_rows, periods, None, global_role_map)

    # --- Summary tab (rendered LAST — global_role_map is now complete) ---
    summ_rows = _write_summary_tab(sid, periods, global_role_map)
    tab_rows["Summary"] = summ_rows
    # Summary has no row_types — formatting falls back to label-based detection

    # --- Formatting ---
    requests = []
    for sheet_name, sheet_id in sheet_ids.items():
        # Column widths: narrow gutter + wide labels + data columns
        requests.extend([
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2},
                "properties": {"pixelSize": 36}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
                "properties": {"pixelSize": 36}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 4 + len(periods)},
                "properties": {"pixelSize": 100}, "fields": "pixelSize"}},
        ])
    for sheet_name, sheet_id in sheet_ids.items():
        tab_data = tab_rows.get(sheet_name, [])
        if tab_data:
            requests.extend(_build_format_requests(
                sheet_id, tab_data, periods,
                row_types=tab_row_types.get(sheet_name),
                unit_label=unit_label
            ))
    gws_batch_update(sid, requests)

    return sid, url

def main():
    parser = argparse.ArgumentParser(description="Render trees to Google Sheets")
    parser.add_argument("--trees", required=True, help="Path to trees JSON")
    parser.add_argument("--company", required=True, help="Company name")
    args = parser.parse_args()
    
    with open(args.trees) as f:
        raw_trees = json.load(f)
    
    from xbrl_tree import TreeNode
    trees = {}
    for k, v in raw_trees.items():
        if k in ("IS", "BS", "BS_LE", "CF") and isinstance(v, dict):
            trees[k] = TreeNode.from_dict(v)
        else:
            trees[k] = v
    
    sid, url = write_sheets(trees, args.company)
    print(json.dumps({"company": args.company, "url": url}))

if __name__ == '__main__':
    main()
