from xbrl_tree import TreeNode
from gws_utils import gws_batch_update
from sheets.api import gws_create
from sheets.formulas import dcol
from sheets.renderers import (
    _render_revenue_segments, _render_sheet_body, _link_revenue_to_segments,
    _write_sheet_tab, _render_cf_with_separators, _write_summary_tab
)
from sheets.formatting import _build_format_requests

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
