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

def _render_sheet_body(tree, periods, start_row, global_role_map, sheet_name, is_cascade=False):
    """Render a tree into rows. Leaves get values, parents get formulas."""
    if is_cascade:
        layout = _cascade_layout(tree, start_row, 0)
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
    for row_num, indent, node in layout:
        label = ("  " * indent) + node.name
        if node.role:
            global_role_map[node.role] = (sheet_name, row_num)
        
        row = ["", "", label, ""]
        if not node.children:
            for p in periods:
                val = node.values.get(p, 0)
                row.append(round(val) if val else "")
        else:
            child_rows = [(node_row[id(c)], c.weight) for c in node.children]
            for i in range(len(periods)):
                col = dcol(i)
                row.append(_build_weight_formula(col, child_rows))
        rows.append(row)
    return rows

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

def _build_format_requests(sheet_id, rows, periods):
    requests = []
    num_data_cols = len(periods)
    data_start_col = 4
    for row_idx, row in enumerate(rows):
        if len(row) < 3:
            continue
        label = row[2].strip() if isinstance(row[2], str) else ""
        if label == "Check":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": data_start_col, "endColumnIndex": data_start_col + num_data_cols
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.0x;(0.0x);-"}}},
                    "fields": "userEnteredFormat.numberFormat"
                }
            })
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2, "endColumnIndex": 3
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"italic": True}}},
                    "fields": "userEnteredFormat.textFormat.italic"
                }
            })
        elif label == "Metrics":
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                        "startColumnIndex": 2, "endColumnIndex": 3
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"italic": True}}},
                    "fields": "userEnteredFormat.textFormat.italic"
                }
            })
        elif label and label != "$m" and label != "Check":
            has_data = any(isinstance(cell, (int, float)) or (isinstance(cell, str) and cell.startswith("=")) for cell in row[4:])
            if has_data:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                            "startColumnIndex": data_start_col, "endColumnIndex": data_start_col + num_data_cols
                        },
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
                        "fields": "userEnteredFormat.numberFormat"
                    }
                })
    return requests

def write_sheets(trees: dict, company: str) -> tuple[str, str]:
    periods = trees.get("complete_periods", [])
    sid, url, sheet_ids = gws_create(f"{company} — Financial Model", ["IS", "BS", "CF", "Summary"])
    global_role_map = {}
    tab_rows = {}

    # No pre-registered Summary rows — Summary is rendered AFTER IS/BS/CF
    # so global_role_map is complete before Summary formulas are built.

    # --- IS tab ---
    is_tree = trees.get("IS")
    if is_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(is_tree, periods, start_row=len(header_rows)+1, global_role_map=global_role_map, sheet_name="IS", is_cascade=True)
        is_rows = header_rows + body_rows
        tab_rows["IS"] = is_rows
        _write_sheet_tab(sid, "IS", is_rows, periods, is_tree, global_role_map)

    # --- BS tab ---
    bs_tree = trees.get("BS")
    bs_le_tree = trees.get("BS_LE")
    if bs_tree or bs_le_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = []
        current_row = len(header_rows) + 1
        if bs_tree:
            assets_rows = _render_sheet_body(bs_tree, periods, start_row=current_row, global_role_map=global_role_map, sheet_name="BS")
            body_rows += assets_rows
            current_row += len(assets_rows)
            body_rows.append([""] * (4 + len(periods)))
            current_row += 1
        if bs_le_tree:
            le_rows = _render_sheet_body(bs_le_tree, periods, start_row=current_row, global_role_map=global_role_map, sheet_name="BS")
            body_rows += le_rows
        bs_rows = header_rows + body_rows
        tab_rows["BS"] = bs_rows
        _write_sheet_tab(sid, "BS", bs_rows, periods, None, global_role_map)

    # --- CF tab ---
    cf_tree = trees.get("CF")
    if cf_tree:
        header_rows = [[], ["", "", "$m", ""] + list(periods), []]
        body_rows = _render_sheet_body(cf_tree, periods, start_row=len(header_rows)+1, global_role_map=global_role_map, sheet_name="CF")

        current_row = len(header_rows) + len(body_rows) + 1
        body_rows.append([""] * (4 + len(periods)))
        current_row += 1

        # --- Cash Proof ---
        # The calc tree only has duration flows (Net Change = OPCF + INVCF + FINCF + FX).
        # Beginning/Ending Cash are instant-context facts outside the calc tree.
        # Ending Cash = Beginning Cash + Net Change (formula, not hardcoded).
        cf_endc_values = trees.get("cf_endc_values", {})
        sorted_endc_dates = sorted(cf_endc_values.keys())

        begc_row_num = current_row
        begc_row = ["", "", "Beginning Cash", ""]
        for p in periods:
            prev_dates = [d for d in sorted_endc_dates if d < p]
            begc_row.append(round(cf_endc_values[prev_dates[-1]]) if prev_dates else "")
        body_rows.append(begc_row)
        global_role_map["CF_BEGC"] = ("CF", begc_row_num)
        current_row += 1

        netch_row_num = current_row
        netch_ref = global_role_map.get("CF_NETCH")
        netch_row = ["", "", "Net Change in Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            netch_row.append(f"={col}{netch_ref[1]}" if netch_ref else "")
        body_rows.append(netch_row)
        current_row += 1

        endc_row_num = current_row
        endc_row = ["", "", "Ending Cash", ""]
        for i in range(len(periods)):
            col = dcol(i)
            endc_row.append(f"={col}{begc_row_num}+{col}{netch_row_num}")
        body_rows.append(endc_row)
        global_role_map["CF_ENDC"] = ("CF", endc_row_num)

        cf_rows = header_rows + body_rows
        tab_rows["CF"] = cf_rows
        _write_sheet_tab(sid, "CF", cf_rows, periods, None, global_role_map)

    # --- Summary tab (rendered LAST — global_role_map is now complete) ---
    summ_rows = _write_summary_tab(sid, periods, global_role_map)
    tab_rows["Summary"] = summ_rows

    requests = []
    for sheet_name, sheet_id in sheet_ids.items():
        requests.extend([
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2},
                "properties": {"pixelSize": 50}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
        ])
    for sheet_name, sheet_id in sheet_ids.items():
        tab_data = tab_rows.get(sheet_name, [])
        if tab_data:
            requests.extend(_build_format_requests(sheet_id, tab_data, periods))
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
