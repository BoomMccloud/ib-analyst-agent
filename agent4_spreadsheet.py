"""
Agent 4: Spreadsheet Builder
=============================
Copies the template spreadsheet, then populates:
1. Annual sheet — raw financial data from Agent 2 (IS/BS/CF)
2. Select KPIs sheet — revenue buildup from Agent 3
3. P&L metrics rows — forecast assumptions from Agent 3

Uses the template_row_map.json to know which rows are INPUT vs FORMULA.

Usage:
  python agent4_spreadsheet.py --model model.json --financials structured.json --company "Apple Inc."
"""

import argparse
import json
import sys

from gws_utils import _run_gws, gws_write, gws_batch_update


TEMPLATE_ROW_MAP = "template_row_map.json"


def gws_clear(sid, range_):
    params = json.dumps({"spreadsheetId": sid, "range": range_})
    _run_gws("sheets", "spreadsheets", "values", "clear", "--params", params, "--json", "{}")


def copy_template(template_id: str, title: str) -> tuple[str, str]:
    """Copy the template to a new spreadsheet."""
    new = _run_gws("sheets", "spreadsheets", "create", "--json", json.dumps({
        "properties": {"title": title}
    }))
    new_id = new["spreadsheetId"]
    default_sheet_id = new["sheets"][0]["properties"]["sheetId"]

    # Get source sheets
    source = _run_gws("sheets", "spreadsheets", "get", "--params", json.dumps({
        "spreadsheetId": template_id,
        "fields": "sheets.properties"
    }))

    # Copy each sheet
    rename_requests = []
    for sheet in source["sheets"]:
        title_s = sheet["properties"]["title"]
        sid = sheet["properties"]["sheetId"]
        params = json.dumps({"spreadsheetId": template_id, "sheetId": sid})
        body = json.dumps({"destinationSpreadsheetId": new_id})
        copy_result = _run_gws("sheets", "spreadsheets", "sheets", "copyTo",
                               "--params", params, "--json", body)
        rename_requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": copy_result["sheetId"],
                    "title": title_s,
                    "index": sheet["properties"]["index"]
                },
                "fields": "title,index"
            }
        })

    rename_requests.append({"deleteSheet": {"sheetId": default_sheet_id}})
    gws_batch_update(new_id, rename_requests)

    url = f"https://docs.google.com/spreadsheets/d/{new_id}/edit"
    return new_id, url


def populate_annual(sid: str, financials: dict, row_map: list[dict]):
    """Populate the Annual sheet with structured financial data.

    The Annual sheet has 3 sections:
    - Rows 3-44: Balance Sheet
    - Rows 48-63: Income Statement
    - Rows 67-108: Cash Flow Statement

    We match row labels to our structured data keys.
    """
    # Build a label→row mapping
    label_to_row = {}
    for entry in row_map:
        if entry["label"]:
            label_to_row[entry["label"].lower().strip()] = entry["row"]

    # Get income statement data — handle nested structures
    is_raw = financials.get("income_statement", {})
    # Could be {fiscal_years: {date: data}} or {income_statement: {date: data}} or {date: data}
    is_inner = is_raw.get("fiscal_years", is_raw.get("income_statement", is_raw))

    # Get balance sheet data
    bs_raw = financials.get("balance_sheet", {})
    bs_inner = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))

    # Get cash flow data
    cf_data = financials.get("cash_flows", {})

    # Determine periods from the data
    periods = []
    for key in is_inner:
        if isinstance(key, str) and len(key) >= 4 and key[:4].isdigit():
            periods.append(key)
    periods.sort()

    if not periods:
        print("  WARNING: No periods found in income statement data", file=sys.stderr)
        return

    print(f"  Periods found: {periods}", file=sys.stderr)

    # Write year headers (row 2 for BS, row 47 for IS, row 66 for CF)
    year_labels = [p[:4] if len(p) == 4 else f"As of {p}" for p in periods]

    # Map our structured data to Annual sheet rows
    # This is the key mapping step — match extracted line items to template rows

    # Income Statement mapping (rows 48-60)
    # Each value is a list of exact keys to try, in priority order
    is_mapping = {
        48: ["total_net_sales", "revenues", "revenue", "total_revenue", "net_sales"],
        50: ["total_cost_of_sales", "cost_of_revenues", "cost_of_revenue", "total_cost_of_revenue"],
        51: ["research_and_development"],
        52: ["selling_general_and_administrative", "sales_and_marketing"],
        53: ["general_and_administrative"],
        54: [],  # Other (European Commission fines for Google)
        55: ["total_costs_and_expenses", "total_operating_expenses"],
        56: ["operating_income", "income_from_operations"],
        57: ["other_income_expense_net", "other_income"],
        58: ["income_before_provision_for_income_taxes", "income_before_income_taxes"],
        59: ["provision_for_income_taxes", "income_tax_expense"],
        60: ["net_income"],
    }

    # Cash Flow mapping (rows 68-105)
    cf_mapping = {
        68: ["net_income"],
        70: ["depreciation_and_amortization", "depreciation_and_impairment"],
        72: ["share_based_compensation_expense", "stock_based_compensation"],
        73: ["deferred_income_taxes"],
        84: ["cash_generated_by_operating_activities", "net_cash_provided_by_operating_activities"],
        86: ["payments_for_acquisition_of_property_plant_and_equipment", "purchases_of_property_and_equipment"],
        94: ["cash_generated_by_used_in_investing_activities", "net_cash_used_in_investing_activities"],
        101: ["cash_used_in_financing_activities", "net_cash_used_in_financing_activities"],
        103: ["increase_decrease_in_cash_cash_equivalents_and_restricted_cash_and_cash_equivalents", "net_increase_decrease"],
        104: ["beginning_cash", "cash_at_beginning"],
        105: ["ending_cash", "cash_at_end"],
    }

    def find_value(data: dict, period: str, key_patterns) -> str:
        """Search for a value in nested data using a list of exact key patterns."""
        if not key_patterns:
            return ""
        patterns = key_patterns if isinstance(key_patterns, list) else key_patterns.split("|")

        # If data is keyed by period at top level
        period_data = data.get(period, {})
        if isinstance(period_data, dict):
            for pattern in patterns:
                # Exact match first
                val = _exact_search(period_data, pattern)
                if val is not None:
                    return val

        # If data is keyed by field name with period sub-keys
        for pattern in patterns:
            for key, val in data.items():
                normalized_key = key.lower().replace(" ", "_").replace("-", "_")
                if normalized_key == pattern:  # exact match
                    if isinstance(val, dict) and period in val:
                        return val[period]
                    elif isinstance(val, (int, float)):
                        return val

        return ""

    def _exact_search(d: dict, pattern: str):
        """Search for a key exactly matching pattern in nested dicts."""
        for key, val in d.items():
            normalized = key.lower().replace(" ", "_").replace("-", "_")
            if normalized == pattern:
                if isinstance(val, (int, float)):
                    return val
                elif isinstance(val, dict):
                    for v in val.values():
                        if isinstance(v, (int, float)):
                            return v
            # Recurse into nested dicts but only for container keys
            if isinstance(val, dict) and not normalized == pattern:
                result = _exact_search(val, pattern)
                if result is not None:
                    return result
        return None

    # Build the data to write
    # Start col C (index 2) for Annual sheet (it uses B for years, C+ for data)
    # Actually looking at the original, col C=2018, D=2019
    # Let's write starting from col C

    # Write IS data
    print("  Writing Income Statement to Annual...", file=sys.stderr)
    for row_num, patterns in is_mapping.items():
        values = []
        for period in periods:
            val = find_value(is_inner, period, patterns)
            values.append(val)
        if any(v != "" for v in values):
            row_data = [[""] * (len(periods))]
            row_data[0] = values
            col_start = chr(65 + 2)  # C
            col_end = chr(65 + 2 + len(periods) - 1)
            gws_write(sid, f"'Annual'!{col_start}{row_num}:{col_end}{row_num}", [values])

    # Write CF data
    print("  Writing Cash Flow to Annual...", file=sys.stderr)
    cf_inner = cf_data
    # Handle nested CF structure
    if "operating_activities" in cf_data:
        # Flatten the nested CF structure
        flat_cf = {}
        for section_key, section_data in cf_data.items():
            if isinstance(section_data, dict):
                for k, v in section_data.items():
                    if isinstance(v, dict):
                        flat_cf[k] = v
                    else:
                        flat_cf[section_key] = section_data
                        break
        cf_inner = flat_cf if flat_cf else cf_data

    for row_num, patterns in cf_mapping.items():
        values = []
        for period in periods:
            val = find_value(cf_inner, period, patterns)
            values.append(val)
        if any(v != "" for v in values):
            col_start = chr(65 + 2)
            col_end = chr(65 + 2 + len(periods) - 1)
            gws_write(sid, f"'Annual'!{col_start}{row_num}:{col_end}{row_num}", [values])

    # Write BS data
    print("  Writing Balance Sheet to Annual...", file=sys.stderr)
    bs_mapping = {
        5: ["cash_and_cash_equivalents"],
        8: ["accounts_receivable_net", "accounts_receivable"],
        10: ["inventories", "inventory"],
        12: ["total_current_assets"],
        15: ["property_plant_and_equipment_net", "property_and_equipment"],
        20: ["total_assets"],
        23: ["accounts_payable"],
        29: ["total_current_liabilities"],
        30: ["long_term_debt", "term_debt"],
        36: ["total_liabilities"],
        43: ["total_shareholders_equity", "total_stockholders_equity"],
        44: ["total_liabilities_and_shareholders_equity", "total_liabilities_and_stockholders"],
    }

    for row_num, patterns in bs_mapping.items():
        values = []
        for period in periods:
            val = find_value(bs_inner, period, patterns)
            values.append(val)
        if any(v != "" for v in values):
            col_start = chr(65 + 2)
            col_end = chr(65 + 2 + len(periods) - 1)
            gws_write(sid, f"'Annual'!{col_start}{row_num}:{col_end}{row_num}", [values])

    print(f"  Annual sheet populated with {len(periods)} periods", file=sys.stderr)


def populate_revenue_build(sid: str, model: dict):
    """Populate the Select KPIs / Revenue Build sheet with segment data."""
    rev_model = model.get("revenue_model", {})
    segments = rev_model.get("segments", [])

    if not segments:
        print("  No revenue segments in model, skipping", file=sys.stderr)
        return

    rows = []
    periods = model.get("historical_periods", [])

    # Header
    rows.append(["", "Revenue Build", ""] + periods)
    rows.append([])

    for seg in segments:
        name = seg.get("name", "Unknown")
        hist_rev = seg.get("historical_revenue", {})
        source = seg.get("source", "")

        # Segment header with source as comment
        rows.append(["", f"--- {name} ---", "", "", f"Source: {source}" if source else ""])

        # Revenue line
        rev_row = ["INPUT", f"  {name} Revenue", ""]
        for p in periods:
            rev_row.append(hist_rev.get(p, ""))
        rows.append(rev_row)

        # Growth rate assumptions
        drivers = seg.get("drivers", {})
        for driver_name, driver_data in drivers.items():
            if not isinstance(driver_data, dict):
                continue
            assumption = driver_data.get("forecast_assumption", {})
            source_text = assumption.get("source", "") if isinstance(assumption, dict) else ""

            hist = driver_data.get("historical", {})
            driver_row = ["INPUT", f"    {driver_name}", ""]
            for p in periods:
                driver_row.append(hist.get(p, ""))
            rows.append(driver_row)

            # Add source as a comment row if present
            if source_text:
                rows.append(["", f"      ↳ {source_text[:80]}", ""])

        rows.append([])

    # Total Revenue
    total_rev_row_num = len(rows) + 1  # 1-indexed row number
    total_hist = rev_model.get("total_revenue_historical", {})
    total_row = ["FORMULA", "Total Revenue", ""]
    for p in periods:
        total_row.append(total_hist.get(p, ""))
    rows.append(total_row)

    # Write
    end_col = chr(65 + 3 + len(periods) - 1)
    gws_write(sid, f"'Select KPIs'!A1:{end_col}{len(rows)}", rows)
    print(f"  Revenue Build: {len(segments)} segments, {len(rows)} rows (Total Rev at row {total_rev_row_num})", file=sys.stderr)
    return total_rev_row_num


def add_assumption_comments(sid: str, model: dict):
    """Add source citations as notes/comments on assumption cells."""
    # Get sheet IDs for adding notes
    sheet_info = _run_gws("sheets", "spreadsheets", "get", "--params", json.dumps({
        "spreadsheetId": sid,
        "fields": "sheets.properties"
    }))
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheet_info["sheets"]}

    # Collect all sources from model
    notes_requests = []
    kpi_sheet_id = sheet_ids.get("Select KPIs")

    if not kpi_sheet_id:
        return

    # Add a "Sources" section at the bottom of Select KPIs
    sources = []
    for seg in model.get("revenue_model", {}).get("segments", []):
        seg_source = seg.get("source", "")
        if seg_source:
            sources.append(f"{seg['name']}: {seg_source}")
        for driver_name, driver_data in seg.get("drivers", {}).items():
            if isinstance(driver_data, dict):
                assumption = driver_data.get("forecast_assumption", {})
                if isinstance(assumption, dict) and assumption.get("source"):
                    sources.append(f"  {driver_name}: {assumption['source']}")

    for key in ["cogs", "research_and_development", "selling_general_and_administrative"]:
        item = model.get("expense_model", {}).get(key, {})
        if isinstance(item, dict) and item.get("source"):
            sources.append(f"{key}: {item['source']}")

    if sources:
        # Write sources section
        source_rows = [[], ["", "=== ASSUMPTION SOURCES ===", ""]]
        for s in sources:
            source_rows.append(["", s, ""])
        # Find next empty row (after revenue build data)
        gws_write(sid, f"'Select KPIs'!A50:C{50+len(source_rows)}", source_rows)
        print(f"  Added {len(sources)} source citations", file=sys.stderr)


def rebuild_pnl(sid: str, financials: dict, model: dict, kpi_total_rev_row: int):
    """Rewrite the P&L sheet with generic IS line items.

    The P&L shows high-level aggregates:
    - Total Revenue (linked from Select KPIs)
    - Cost of Revenue
    - Gross Profit (formula)
    - R&D, S&M, G&A
    - Total Opex (formula)
    - EBIT (formula)
    - Other Income/Expense
    - EBT, Tax, Net Income
    - Metrics as % of revenue

    Historical data comes from the Annual sheet.
    Forecast assumptions (% of revenue) are INPUT cells.
    """
    # Get periods from the Annual sheet data
    is_raw = financials.get("income_statement", {})
    is_inner = is_raw.get("fiscal_years", is_raw.get("income_statement", is_raw))
    periods = sorted([k for k in is_inner if isinstance(k, str) and k[:4].isdigit()])
    n_hist = len(periods)

    # Forecast periods
    if periods:
        last_year = int(periods[-1][:4])
        forecast_periods = [str(last_year + i) + "E" for i in range(1, 6)]
    else:
        forecast_periods = []

    all_periods = periods + forecast_periods
    n_total = len(all_periods)

    # Helper: col letter for data columns (D=3, E=4, ...)
    def dcol(i):
        return chr(68 + i)  # D=0, E=1, ...

    # Clear the entire P&L sheet first
    gws_clear(sid, "'P&L'!A1:Z80")

    # --- Build rows ---
    rows = []

    # Row 1: blank
    rows.append([])
    # Row 2: Header
    rows.append(["", "$m", ""] + all_periods)
    # Row 3: blank
    rows.append([])

    # Row 4: Total Revenue = linked from Select KPIs
    rev_row = ["FORMULA", "Total Revenue", ""]
    for i in range(n_total):
        c = dcol(i)
        rev_row.append(f"='Select KPIs'!{c}{kpi_total_rev_row}")
    rows.append(rev_row)

    # Row 5: blank
    rows.append([])

    # Row 6: Cost of Revenue (from Annual for historical, % assumption for forecast)
    cogs_row = ["INPUT", "Cost of Revenue", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)  # Annual cols: C, D, E, ...
            cogs_row.append(f"='Annual'!{c}50")
        else:
            # Forecast: use COGS % assumption × revenue
            c = dcol(i)
            cogs_row.append(f"={c}24*{c}4")  # row 24 will be COGS % assumption
    rows.append(cogs_row)

    # Row 7: Gross Profit = Revenue - COGS
    gp_row = ["FORMULA", "Gross Profit", ""]
    for i in range(n_total):
        c = dcol(i)
        gp_row.append(f"={c}4-{c}6")
    rows.append(gp_row)

    # Row 8: blank
    rows.append([])

    # Row 9: R&D
    rd_row = ["INPUT", "Research & Development", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)
            rd_row.append(f"='Annual'!{c}51")
        else:
            c = dcol(i)
            rd_row.append(f"={c}25*{c}4")  # row 25 = R&D % assumption
    rows.append(rd_row)

    # Row 10: S&M
    sm_row = ["INPUT", "Sales & Marketing", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)
            sm_row.append(f"='Annual'!{c}52")
        else:
            c = dcol(i)
            sm_row.append(f"={c}26*{c}4")  # row 26 = S&M %
    rows.append(sm_row)

    # Row 11: G&A
    ga_row = ["INPUT", "General & Administrative", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)
            ga_row.append(f"='Annual'!{c}53")
        else:
            c = dcol(i)
            ga_row.append(f"={c}27*{c}4")  # row 27 = G&A %
    rows.append(ga_row)

    # Row 12: Other opex
    other_row = ["INPUT", "Other Operating Costs", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)
            other_row.append(f"='Annual'!{c}54")
        else:
            other_row.append(0)
    rows.append(other_row)

    # Row 13: Total Opex = SUM(R&D + S&M + G&A + Other)
    opex_row = ["FORMULA", "Total Operating Expenses", ""]
    for i in range(n_total):
        c = dcol(i)
        opex_row.append(f"=SUM({c}9:{c}12)")
    rows.append(opex_row)

    # Row 14: blank
    rows.append([])

    # Row 15: EBIT = Gross Profit - Total Opex
    ebit_row = ["FORMULA", "Operating Income (EBIT)", ""]
    for i in range(n_total):
        c = dcol(i)
        ebit_row.append(f"={c}7-{c}13")
    rows.append(ebit_row)

    # Row 16: Other Income / Expense
    oi_row = ["INPUT", "Other Income / (Expense)", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)
            oi_row.append(f"='Annual'!{c}57")
        else:
            oi_row.append(0)
    rows.append(oi_row)

    # Row 17: EBT = EBIT + Other
    ebt_row = ["FORMULA", "Earnings Before Tax", ""]
    for i in range(n_total):
        c = dcol(i)
        ebt_row.append(f"={c}15+{c}16")
    rows.append(ebt_row)

    # Row 18: Income Tax
    tax_row = ["INPUT", "Income Tax Expense", ""]
    for i, p in enumerate(all_periods):
        if i < n_hist:
            c = chr(67 + i)
            tax_row.append(f"='Annual'!{c}59")
        else:
            c = dcol(i)
            tax_row.append(f"={c}28*{c}17")  # row 28 = effective tax rate assumption
    rows.append(tax_row)

    # Row 19: Net Income = EBT - Tax
    ni_row = ["FORMULA", "Net Income", ""]
    for i in range(n_total):
        c = dcol(i)
        ni_row.append(f"={c}17-{c}18")
    rows.append(ni_row)

    # Row 20: blank
    rows.append([])

    # Row 21: blank
    rows.append([])

    # --- METRICS / ASSUMPTIONS SECTION ---
    # Row 22: Section header
    rows.append(["", "Metrics & Assumptions", ""] + [""] * n_total)

    # Row 23: Revenue YoY Growth
    growth_row = ["FORMULA", "  Revenue YoY Growth %", ""]
    for i in range(n_total):
        if i == 0:
            growth_row.append("")
        else:
            c = dcol(i)
            p = dcol(i - 1)
            growth_row.append(f"=IF({p}4=0,\"\",{c}4/{p}4-1)")
    rows.append(growth_row)

    # Row 24: COGS as % of Revenue (INPUT for forecast)
    cogs_pct_row = ["INPUT", "  COGS as % of Revenue", ""]
    for i in range(n_total):
        c = dcol(i)
        if i < n_hist:
            cogs_pct_row.append(f"=IF({c}4=0,\"\",{c}6/{c}4)")
        else:
            cogs_pct_row.append("")  # user fills forecast assumption
    rows.append(cogs_pct_row)

    # Row 25: R&D as % of Revenue
    rd_pct_row = ["INPUT", "  R&D as % of Revenue", ""]
    for i in range(n_total):
        c = dcol(i)
        if i < n_hist:
            rd_pct_row.append(f"=IF({c}4=0,\"\",{c}9/{c}4)")
        else:
            rd_pct_row.append("")
    rows.append(rd_pct_row)

    # Row 26: S&M as % of Revenue
    sm_pct_row = ["INPUT", "  S&M as % of Revenue", ""]
    for i in range(n_total):
        c = dcol(i)
        if i < n_hist:
            sm_pct_row.append(f"=IF({c}4=0,\"\",{c}10/{c}4)")
        else:
            sm_pct_row.append("")
    rows.append(sm_pct_row)

    # Row 27: G&A as % of Revenue
    ga_pct_row = ["INPUT", "  G&A as % of Revenue", ""]
    for i in range(n_total):
        c = dcol(i)
        if i < n_hist:
            ga_pct_row.append(f"=IF({c}4=0,\"\",{c}11/{c}4)")
        else:
            ga_pct_row.append("")
    rows.append(ga_pct_row)

    # Row 28: Effective Tax Rate
    etr_row = ["INPUT", "  Effective Tax Rate", ""]
    for i in range(n_total):
        c = dcol(i)
        if i < n_hist:
            etr_row.append(f"=IF({c}17=0,\"\",{c}18/{c}17)")
        else:
            etr_row.append("")
    rows.append(etr_row)

    # Row 29: Gross Margin %
    gm_row = ["FORMULA", "  Gross Margin %", ""]
    for i in range(n_total):
        c = dcol(i)
        gm_row.append(f"=IF({c}4=0,\"\",{c}7/{c}4)")
    rows.append(gm_row)

    # Row 30: EBIT Margin %
    em_row = ["FORMULA", "  EBIT Margin %", ""]
    for i in range(n_total):
        c = dcol(i)
        em_row.append(f"=IF({c}4=0,\"\",{c}15/{c}4)")
    rows.append(em_row)

    # Row 31: Net Margin %
    nm_row = ["FORMULA", "  Net Margin %", ""]
    for i in range(n_total):
        c = dcol(i)
        nm_row.append(f"=IF({c}4=0,\"\",{c}19/{c}4)")
    rows.append(nm_row)

    # Write everything
    end_col = dcol(n_total - 1)
    gws_write(sid, f"'P&L'!A1:{end_col}{len(rows)}", rows)
    print(f"  P&L rebuilt: {len(rows)} rows, {n_hist} historical + {len(forecast_periods)} forecast periods", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Agent 4: Populate template with financial data")
    parser.add_argument("--model", required=True, help="Model JSON from Agent 3")
    parser.add_argument("--financials", required=True, help="Structured financials from Agent 2")
    parser.add_argument("--company", default=None, help="Company name")
    parser.add_argument("--template-map", default=TEMPLATE_ROW_MAP, help="Template row map JSON")
    args = parser.parse_args()

    with open(args.model) as f:
        model = json.load(f)
    with open(args.financials) as f:
        financials = json.load(f)
    with open(args.template_map) as f:
        template = json.load(f)

    company = args.company or model.get("company", "Unknown")
    template_id = template["template_id"]

    # Step 1: Copy template
    print(f"Step 1: Copying template for {company}...", file=sys.stderr)
    new_id, url = copy_template(template_id, f"{company} - Financial Model")
    print(f"  URL: {url}", file=sys.stderr)

    # Step 2: Populate Annual sheet
    print(f"\nStep 2: Populating Annual sheet...", file=sys.stderr)
    populate_annual(new_id, financials, template["sheets"]["Annual"])

    # Step 3: Populate Revenue Build (Select KPIs)
    print(f"\nStep 3: Populating Revenue Build...", file=sys.stderr)
    kpi_total_row = populate_revenue_build(new_id, model)

    # Step 4: Rebuild P&L with generic IS line items
    print(f"\nStep 4: Rebuilding P&L...", file=sys.stderr)
    rebuild_pnl(new_id, financials, model, kpi_total_row)

    # Step 5: Add source citations
    print(f"\nStep 5: Adding assumption sources...", file=sys.stderr)
    add_assumption_comments(new_id, model)

    print(f"\nDone!", file=sys.stderr)
    print(f"  URL: {url}", file=sys.stderr)
    print(json.dumps({"spreadsheet_id": new_id, "url": url, "company": company}, indent=2))


if __name__ == "__main__":
    main()
