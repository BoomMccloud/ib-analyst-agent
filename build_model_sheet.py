"""
Generic 3-Statement Financial Model Builder
=============================================
Architecture: Model structure is defined first (fixed codes, invariants, formulas).
Filing data is classified to match. IS/BS/CF pull from Filing via SUMIF.

Invariants enforced:
  IS:  GP = REVT - COGST, OPINC = GP - OPEXT, EBT = OPINC + INC_O, INC_NET = EBT - TAX
  BS:  BS_TCA = sum(cash..ca3), BS_TNCA = sum(ppe..lta2), BS_TA = BS_TCA + BS_TNCA
       BS_TCL = sum(ap..cl3), BS_TNCL = sum(ltd..ll2), BS_TL = BS_TCL + BS_TNCL
       BS_TE = CS + RE + OE, BALANCE CHECK: BS_TA = BS_TL + BS_TE
  CF:  Ending Cash = Beginning + Net Change, feeds back to BS_CASH

Usage:
  python build_model_sheet.py --financials /tmp/aapl_all_structured.json --company "Apple Inc."
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

from anthropic import Anthropic

from gws_utils import _run_gws, gws_write, gws_batch_update
from llm_utils import parse_json_response
from financial_utils import (
    BS_CODE_DEFS,
    CF_CODE_DEFS,
    flatten_bs,
    flatten_cf,
    clean_label,
)

# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def gws_create(title, sheet_names):
    sheets = [{"properties": {"title": n, "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 4, "hideGridlines": True}}} for n in sheet_names]
    result = _run_gws("sheets", "spreadsheets", "create", "--json", json.dumps({
        "properties": {"title": title}, "sheets": sheets
    }))
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in result["sheets"]}
    return result["spreadsheetId"], result["spreadsheetUrl"], sheet_ids


def dcol(i):
    """Data column letter(s). i=0 → D, i=1 → E, etc."""
    col_num = i + 4
    result = ""
    while col_num >= 0:
        result = chr(65 + col_num % 26) + result
        col_num = col_num // 26 - 1
    return result


def col_range(n):
    return dcol(n - 1)


# Column layout: A=code, B=reserved, C=label, D=spacer, E+=data
DATA_COL_OFFSET = 4  # index into row list where data starts


def R(code, label):
    """Row prefix: [code, reserved, label, spacer]."""
    return [code, "", label, ""]


def sumif_formula(code, col_i):
    """SUMIF pulling from Filing by code. Use {row} placeholder for the row."""
    c = dcol(col_i)
    return f'=IFERROR(SUMIF(Filing!$A:$A,$A{{row}},Filing!{c}:{c}),"")'



def label_formula():
    """INDEX/MATCH to pull label from Filing by code in $A."""
    return '=IFERROR(INDEX(Filing!C:C,MATCH($A{row},Filing!$A:$A,0)),"")'


# ---------------------------------------------------------------------------
# MODEL STRUCTURE — the source of truth
# ---------------------------------------------------------------------------
# Each entry: (code, default_label, type)
#   type: "sumif"   = pull from Filing via SUMIF (historical), input/driver (forecast)
#         "sum"     = formula summing other rows (always computed)
#         "formula" = specific formula (always computed)
#         "label"   = section header, no data
#         "blank"   = empty row

IS_STRUCTURE = [
    ("", "Revenue", "label"),
    ("REV1", None, "sumif"),     # None label = pull from Filing via INDEX/MATCH
    ("REV2", None, "sumif"),
    ("REV3", None, "sumif"),
    ("REVT", "Total Revenue", "sum"),  # = REV1 + REV2 + REV3
    ("", "", "blank"),
    ("", "Metrics and drivers - Revenue", "label"),
    ("", "YoY Revenue Growth %", "driver_rev_growth"),
    ("", "", "blank"),
    ("", "Cost of Revenue", "label"),
    ("COGS1", None, "sumif"),
    ("COGS2", None, "sumif"),
    ("COGS3", None, "sumif"),
    ("COGST", "Total Cost of Revenue", "sum"),  # = COGS1 + COGS2 + COGS3
    ("GP", "Gross Profit", "formula"),  # = REVT - COGST
    ("", "", "blank"),
    ("", "Metrics and drivers - COGS", "label"),
    ("", "COGS as % of Revenue", "driver_cogs_pct"),
    ("", "", "blank"),
    ("", "Operating Expenses", "label"),
    ("OPEX1", None, "sumif"),
    ("OPEX2", None, "sumif"),
    ("OPEX3", None, "sumif"),
    ("OPEXT", "Total Operating Expenses", "sum"),  # = OPEX1 + OPEX2 + OPEX3
    ("", "", "blank"),
    ("SBC", "Stock-Based Compensation", "sumif"),
    ("", "", "blank"),
    ("", "Metrics and drivers - Operating Costs", "label"),
    ("", "OPEX1 as % of Revenue", "driver_opex1_pct"),
    ("", "OPEX2 as % of Revenue", "driver_opex2_pct"),
    ("", "OPEX3 as % of Revenue", "driver_opex3_pct"),
    ("", "SBC as % of OpEx", "driver_sbc_pct"),
    ("", "", "blank"),
    ("OPINC", "Operating Income (EBIT)", "formula"),  # = GP - OPEXT
    ("", "", "blank"),
    ("INC_O", "Other Income / (Expense)", "sumif"),
    ("EBT", "Earnings Before Tax (EBT)", "formula"),  # = OPINC + INC_O
    ("", "", "blank"),
    ("TAX", "Income Tax", "sumif"),  # hist=sumif, forecast=EBT*rate
    ("INC_NET", "Net Income", "formula"),  # = EBT - TAX
    ("", "", "blank"),
    ("", "Metrics and drivers - Tax", "label"),
    ("", "Effective Tax Rate %", "driver_tax_rate"),
    ("", "", "blank"),
    ("DA", "Depreciation & Amortization", "sumif"),
    ("", "", "blank"),
    ("", "Margins", "label"),
    ("", "  Gross Margin %", "margin_gp"),
    ("", "  EBIT Margin %", "margin_ebit"),
    ("", "  Net Margin %", "margin_ni"),
]

# BS: fixed layout with sum invariants
BS_ASSETS = [
    ("BS_CASH", "Cash & Equivalents", "cash"),
    ("BS_AR", "Accounts Receivable", "days_rev"),
    ("BS_INV", "Inventories", "days_cogs"),
    ("BS_CA1", "Current Asset 1", "sumif_hold"),
    ("BS_CA2", "Current Asset 2", "sumif_hold"),
    ("BS_CA3", "Current Asset 3", "sumif_hold"),
    ("BS_TCA", "Total Current Assets", "sum"),       # INVARIANT: = sum of above
]

BS_NONCURRENT_ASSETS = [
    ("BS_PPE", "PP&E, net", "ppe_rollforward"),
    ("BS_LTA1", "Long-term Asset 1", "sumif_hold"),
    ("BS_LTA2", "Long-term Asset 2", "sumif_hold"),
    ("BS_TNCA", "Total Non-Current Assets", "sum"),  # INVARIANT: = sum of above
]

BS_TOTAL_ASSETS = ("BS_TA", "Total Assets", "sum")    # INVARIANT: = BS_TCA + BS_TNCA

BS_CURRENT_LIABILITIES = [
    ("BS_AP", "Accounts Payable", "days_costs"),
    ("BS_STD", "Short-term Debt", "sumif_hold"),
    ("BS_OCL1", "Other Current Liability 1", "sumif_hold"),
    ("BS_OCL2", "Other Current Liability 2", "sumif_hold"),
    ("BS_TCL", "Total Current Liabilities", "sum"),  # INVARIANT: = sum of above
]

BS_NONCURRENT_LIABILITIES = [
    ("BS_LTD", "Long-term Debt", "sumif_hold"),
    ("BS_NCL1", "Non-Current Liability 1", "sumif_hold"),
    ("BS_NCL2", "Non-Current Liability 2", "sumif_hold"),
    ("BS_TNCL", "Total Non-Current Liabilities", "sum"),  # INVARIANT: = sum of above
]

BS_TOTAL_LIABILITIES = ("BS_TL", "Total Liabilities", "sum")  # INVARIANT: = BS_TCL + BS_TNCL

BS_EQUITY = [
    ("BS_CS", "Common Stock & APIC", "equity_cs"),
    ("BS_RE", "Retained Earnings", "equity_re"),
    ("BS_OE", "Other Equity (AOCI etc)", "sumif_hold"),
    ("BS_TE", "Total Stockholders' Equity", "sum"),  # INVARIANT: = sum of above
]


# ---------------------------------------------------------------------------
# FILING DATA CLASSIFICATION — map company data to model codes
# ---------------------------------------------------------------------------

HAIKU = "claude-haiku-4-5-20251001"


def _llm_classify(items, code_defs, statement_type):
    """Use LLM to assign model codes to financial line items."""
    client = Anthropic()

    items_for_prompt = [{"id": it["id"], "key": it["key"], "section": it["section"]} for it in items]

    prompt = f"""Assign each {statement_type} line item to exactly one model code.

MODEL CODES:
{json.dumps(code_defs, indent=2)}

LINE ITEMS:
{json.dumps(items_for_prompt, indent=2)}

RULES:
1. Every item MUST be assigned exactly one code.
2. Multiple items CAN share the same code (they will be summed).
3. SUBTOTAL/GRAND TOTAL codes must ONLY be used for actual totals.
4. Catch-all buckets absorb items that don't fit a specific code.
5. Spread items across available catch-all buckets by category (e.g., non-cash adjustments in CF_OP1, asset WC in CF_OP2, liability WC in CF_OP3).
6. Use "section" to understand which part of the statement an item belongs to.
7. SKIP supplemental disclosures — items like "cash paid for income taxes", "cash paid for interest", "non-cash investing/financing activities", and "right-of-use asset" are NOT actual cash flow items. They are informational footnotes already embedded in the numbers above. Assign them "SKIP".

Return ONLY a JSON object mapping each item "id" to its assigned code. No explanation."""

    for attempt in range(2):
        response = client.messages.create(
            model=HAIKU,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        try:
            return parse_json_response(text, response.stop_reason)
        except ValueError:
            if attempt == 0:
                print(f"  LLM classify retry ({statement_type})...", file=sys.stderr)
                continue
            raise ValueError(f"Failed to parse LLM classification for {statement_type}")


def _navigate_path(data, path):
    current = data
    for key in path:
        if isinstance(current, dict) and key in current and isinstance(current[key], dict):
            current = current[key]
        else:
            return None
    return current


def _find_value(data, keys, paths=None):
    if paths:
        for path in paths:
            target = _navigate_path(data, path)
            if target:
                for k in keys:
                    if k in target and isinstance(target[k], (int, float)):
                        return target[k]
        return None
    for k in keys:
        if k in data and isinstance(data[k], (int, float)):
            return data[k]
    for v in data.values():
        if isinstance(v, dict):
            result = _find_value(v, keys)
            if result is not None:
                return result
    return None


def _convert_section_first(data):
    """Convert section-first BS format to period-first."""
    periods = set()
    def collect(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, dict):
                    pkeys = [pk for pk in v if isinstance(pk, str) and len(pk) >= 4
                             and pk[:4].isdigit() and not isinstance(v[pk], dict)]
                    if pkeys:
                        periods.update(pkeys)
                    else:
                        collect(v)
    collect(data)
    if not periods:
        return data, []
    sorted_p = sorted(periods)
    skip = {"unit", "company", "statement", "currencies", "note", "currency_note",
            "note_2a", "ads_conversion_note", "period_end_month"}
    def extract(obj, period):
        if isinstance(obj, dict):
            if period in obj and not isinstance(obj[period], dict):
                return obj[period]
            result = {}
            for k, v in obj.items():
                if k in skip:
                    continue
                ex = extract(v, period)
                if ex is not None:
                    result[k] = ex
            return result if result else None
        return None
    out = {}
    for p in sorted_p:
        ex = extract(data, p)
        if ex:
            out[p] = ex
    return out, sorted_p


def _merge_financials(financials_list: list[dict]) -> dict:
    """Merge multiple structured financials dicts. Later entries take priority for overlapping periods."""
    if len(financials_list) == 1:
        return financials_list[0]

    merged = {}
    for fin in financials_list:
        for section_key in fin:
            if section_key not in merged:
                merged[section_key] = fin[section_key]
                continue
            # Deep merge: for period-keyed dicts, add missing periods
            _deep_merge(merged[section_key], fin[section_key])
    return merged


def _deep_merge(base, overlay):
    """Recursively merge overlay into base. Base values take priority (newer filing)."""
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return
    for k, v in overlay.items():
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)


def classify_filing(financials: dict) -> dict:
    """Map structured financials to model codes. Returns {periods, rows: [{code, label, values}]}."""

    # --- IS ---
    is_raw = financials.get("income_statement", {})
    is_data = is_raw.get("fiscal_years", is_raw.get("data", is_raw))
    periods = sorted([k for k in is_data if isinstance(is_data.get(k), dict)
                      and k[:4].isdigit() and not k.lower().endswith("_usd")])

    def get_is(keys, paths=None):
        vals = {}
        for p in periods:
            v = _find_value(is_data.get(p, {}), keys, paths)
            if v is not None:
                vals[p] = v
        return vals

    rows = []
    def add(code, label, vals):
        if vals:
            rows.append({"code": code, "label": label, "values": vals})

    # Revenue
    rev_subs = [
        (get_is(["products"], [["net_sales"], ["revenue"]]), "Revenue - Products"),
        (get_is(["services"], [["net_sales"], ["revenue"]]), "Revenue - Services"),
    ]
    rev_subs = [(v, l) for v, l in rev_subs if v]
    for i, (v, l) in enumerate(rev_subs):
        code = f"REV{min(i+1, 3)}"
        add(code, l, v)
    add("REVT", "Total Revenue", get_is(["total_net_sales", "revenues", "revenue", "total_revenue", "net_revenues"]))

    # COGS
    cogs_subs = [
        (get_is(["products"], [["cost_of_sales"], ["cost_of_revenue"]]), "COGS - Products"),
        (get_is(["services"], [["cost_of_sales"], ["cost_of_revenue"]]), "COGS - Services"),
    ]
    cogs_subs = [(v, l) for v, l in cogs_subs if v]
    for i, (v, l) in enumerate(cogs_subs):
        code = f"COGS{min(i+1, 3)}"
        add(code, l, v)
    add("COGST", "Cost of Revenue", get_is(["total_cost_of_sales", "cost_of_revenues", "cost_of_revenue", "cost_of_goods_sold"]))

    add("GP", "Gross Profit", get_is(["gross_margin", "gross_profit"]))

    # OpEx
    opex_subs = [
        (get_is(["research_and_development", "research_and_development_expense", "product_development_expenses"], [["operating_expenses"]]), "Research & Development"),
        (get_is(["selling_general_and_administrative"], [["operating_expenses"]]), "Selling, General & Admin"),
        (get_is(["sales_and_marketing", "sales_and_marketing_expenses"], [["operating_expenses"]]), "Sales & Marketing"),
        (get_is(["general_and_administrative", "general_and_administrative_expenses"], [["operating_expenses"]]), "General & Administrative"),
    ]
    opex_subs = [(v, l) for v, l in opex_subs if v]
    for i, (v, l) in enumerate(opex_subs):
        code = f"OPEX{min(i+1, 3)}"
        add(code, l, v)
    add("OPEXT", "Total Operating Expenses", get_is(["total_operating_expenses"], [["operating_expenses"]]))

    add("OPINC", "Operating Income", get_is(["operating_income", "income_from_operations"]))
    add("INC_O", "Other Income / (Expense)", get_is(["other_income_expense_net", "other_income_net"]))
    add("EBT", "Earnings Before Tax", get_is(["income_before_provision_for_income_taxes", "income_before_income_taxes",
                                               "income_before_income_tax_and_share_of_results_of_equity_method_investees"]))
    add("TAX", "Income Tax", get_is(["provision_for_income_taxes", "income_tax_expense", "income_tax_expenses"]))
    add("INC_NET", "Net Income", get_is(["net_income"]))

    # SBC & D&A
    cf_data = financials.get("cash_flows", {})
    op_cf = cf_data.get("operating_activities", cf_data.get("cash_flows_from_operating_activities", {}))
    adj = op_cf.get("adjustments_to_reconcile_net_income", {})

    def get_cf_item(section, keys):
        for k in keys:
            if k in section and isinstance(section[k], dict):
                vals = {p: section[k][p] for p in periods if p in section[k]}
                if vals:
                    return vals
        return {}

    sbc = get_is(["share_based_compensation_expense", "stock_based_compensation"])
    if not sbc:
        sbc = get_cf_item(adj, ["share_based_compensation_expense", "stock_based_compensation"])
    add("SBC", "Stock-Based Compensation", sbc)

    da = get_is(["depreciation_and_amortization"])
    if not da:
        da = get_cf_item(adj, ["depreciation_and_amortization"])
    add("DA", "Depreciation & Amortization", da)

    # --- BS ---
    bs_raw = financials.get("balance_sheet", {})
    coded_bs = bs_raw.get("_coded_items")
    if coded_bs:
        for item in coded_bs:
            add(item["code"], item["label"], item["values"])
    else:
        bs_data = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
        bs_pkeys = [k for k in bs_data if isinstance(bs_data.get(k), dict) and k[:4].isdigit() and not k.lower().endswith("_usd")]
        if bs_pkeys:
            bs_periods = sorted(bs_pkeys)
        else:
            bs_data, bs_periods = _convert_section_first(bs_data)
            bs_periods = [p for p in bs_periods if not p.lower().endswith("_usd")]
        print("  Classifying BS items (no pre-computed codes)...", file=sys.stderr)
        bs_items = flatten_bs(bs_data, bs_periods)
        if bs_items:
            bs_mapping = _llm_classify(bs_items, BS_CODE_DEFS, "Balance Sheet")
            for item in bs_items:
                code = bs_mapping.get(item["id"])
                if code and code != "SKIP":
                    add(code, clean_label(item["key"]), item["values"])

    # --- CF ---
    coded_cf = cf_data.get("_coded_items")
    if coded_cf:
        for item in coded_cf:
            add(item["code"], item["label"], item["values"])
    else:
        print("  Classifying CF items (no pre-computed codes)...", file=sys.stderr)
        cf_items = flatten_cf(cf_data, periods)
        if cf_items:
            cf_mapping = _llm_classify(cf_items, CF_CODE_DEFS, "Cash Flow Statement")
            for item in cf_items:
                code = cf_mapping.get(item["id"])
                if code and code != "SKIP":
                    add(code, clean_label(item["key"]), item["values"])

    return {"periods": periods, "rows": rows}


def build_filing_sheet(sid, filing_data, periods, code_map=None):
    out = [[], R("", "Filing Data") + ["'" + p for p in periods], []]
    section_breaks = {"REVT": "INCOME STATEMENT", "BS_CASH": "BALANCE SHEET", "CF_NI": "CASH FLOWS"}
    seen = set()
    current_section = "IS"
    row_sections = []
    for item in filing_data["rows"]:
        sec = section_breaks.get(item["code"])
        if sec and sec not in seen:
            if sec == "BALANCE SHEET": current_section = "BS"
            elif sec == "CASH FLOWS": current_section = "CF"
            out.append([])
            out.append(["", "", sec])
            seen.add(sec)
        code = item["code"]
        if code_map and code in code_map:
            sheet, row_num = code_map[code]
            row_data = R(f"={sheet}!A{row_num}", item["label"])
        else:
            row_data = R(code, item["label"])
        for p in periods:
            row_data.append(item["values"].get(p, ""))
        row_sections.append((len(out), current_section))
        out.append(row_data)
    gws_write(sid, f"Filing!A1:{col_range(len(periods))}{len(out)}", out)
    return row_sections


def apply_filing_validation(sid, sheet_ids, row_sections):
    filing_sheet_id = sheet_ids["Filing"]
    source_sheets = {"IS": sheet_ids["IS"], "BS": sheet_ids["BS"], "CF": sheet_ids["CF"]}
    if not row_sections: return
    ranges = []
    cur_start, cur_section = row_sections[0]
    cur_end = cur_start + 1
    for row_idx, section in row_sections[1:]:
        if section == cur_section and row_idx <= cur_end + 2:
            cur_end = row_idx + 1
        else:
            ranges.append((cur_start, cur_end, cur_section))
            cur_start, cur_section = row_idx, section
            cur_end = row_idx + 1
    ranges.append((cur_start, cur_end, cur_section))
    requests = []
    for start, end, section in ranges:
        requests.append({"setDataValidation": {"range": {"sheetId": filing_sheet_id, "startRowIndex": start, "endRowIndex": end, "startColumnIndex": 0, "endColumnIndex": 1},
                "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"={section}!$A:$A"}]}, "showCustomUi": True, "strict": True}}})
    gws_batch_update(sid, requests)


def build_is_sheet(sid, periods, forecast_periods):
    all_p = periods + forecast_periods
    nh = len(periods)
    n = len(all_p)
    rows = [[] , R("", "$m") + all_p, []]
    refs = {}
    sum_groups = {}
    for code, default_label, rtype in IS_STRUCTURE:
        if rtype == "blank": rows.append([]); continue
        if rtype == "label": rows.append(["", "", default_label]); continue
        row_num = len(rows) + 1
        if rtype == "sumif":
            label_cell = label_formula().replace("{row}", str(row_num)) if default_label is None else default_label
            d = R(code, label_cell)
            for i in range(n):
                d.append(sumif_formula(code, i).replace("{row}", str(row_num)) if i < nh else "")
            rows.append(d); refs[code] = row_num
            if code.startswith("REV") and code != "REVT": sum_groups.setdefault("REVT", []).append(row_num)
            elif code.startswith("COGS") and code != "COGST": sum_groups.setdefault("COGST", []).append(row_num)
            elif code.startswith("OPEX") and code != "OPEXT": sum_groups.setdefault("OPEXT", []).append(row_num)
        elif rtype == "sum":
            d = R(code, default_label)
            component_rows = sum_groups.get(code, [])
            for i in range(n):
                c = dcol(i)
                if component_rows: d.append(f"={'+'.join(f'{c}{cr}' for cr in component_rows)}")
                elif i < nh: d.append(sumif_formula(code, i).replace("{row}", str(row_num)))
                else: d.append("")
            rows.append(d); refs[code] = row_num
        elif rtype == "formula":
            d = R(code, default_label)
            for i in range(n):
                c = dcol(i)
                if code == "GP": d.append(f"={c}{refs['REVT']}-{c}{refs['COGST']}")
                elif code == "OPINC": d.append(f"={c}{refs['GP']}-{c}{refs['OPEXT']}")
                elif code == "EBT": d.append(f"={c}{refs['OPINC']}+{c}{refs['INC_O']}")
                elif code == "INC_NET": d.append(f"={c}{refs['EBT']}-{c}{refs['TAX']}")
            rows.append(d); refs[code] = row_num
        elif rtype.startswith("driver_"):
            d = R("", default_label)
            for i in range(n):
                c = dcol(i)
                if rtype == "driver_rev_growth":
                    if i == 0: d.append("")
                    elif i < nh: d.append(f"={c}{refs['REVT']}/{dcol(i-1)}{refs['REVT']}-1")
                    else: d.append(0.05)
                elif rtype == "driver_cogs_pct":
                    if i < nh: d.append(f"=IF({c}{refs['REVT']}=0,\"\",{c}{refs['COGST']}/{c}{refs['REVT']})")
                    else: d.append(0.45)
                elif rtype.startswith("driver_opex") and rtype.endswith("_pct"):
                    slot = rtype.replace("driver_opex", "").replace("_pct", "")
                    opex_code = f"OPEX{slot}"
                    if opex_code in refs:
                        if i < nh: d.append(f"=IF({c}{refs['REVT']}=0,\"\",{c}{refs[opex_code]}/{c}{refs['REVT']})")
                        else: d.append(0.10)
                    else: d.append("" if i < nh else 0)
                elif rtype == "driver_sbc_pct":
                    if i < nh: d.append(f"=IF({c}{refs['OPEXT']}=0,\"\",{c}{refs['SBC']}/{c}{refs['OPEXT']})")
                    else: d.append(0.20)
                elif rtype == "driver_tax_rate":
                    if i < nh: d.append(f"=IF({c}{refs['EBT']}=0,\"\",{c}{refs['TAX']}/{c}{refs['EBT']})")
                    else: d.append(0.21)
            rows.append(d); refs[rtype] = row_num
        elif rtype.startswith("margin_"):
            d = R("", default_label)
            src_map = {"margin_gp": "GP", "margin_ebit": "OPINC", "margin_ni": "INC_NET"}
            src = refs.get(src_map.get(rtype, ""))
            for i in range(n):
                c = dcol(i)
                d.append(f"=IF({c}{refs['REVT']}=0,\"\",{c}{src}/{c}{refs['REVT']})" if src else "")
            rows.append(d); refs[rtype] = len(rows)

    growth_row = refs["driver_rev_growth"]
    for i in range(nh, n):
        c = dcol(i)
        rows[refs["REVT"] - 1][4 + i] = f"={dcol(i-1)}{refs['REVT']}*(1+{c}{growth_row})"
    cogs_pct_row = refs["driver_cogs_pct"]
    if not sum_groups.get("COGST"):
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["COGST"] - 1][4 + i] = f"={c}{cogs_pct_row}*{c}{refs['REVT']}"
    for slot in [1, 2, 3]:
        opex_code, drv_key = f"OPEX{slot}", f"driver_opex{slot}_pct"
        if opex_code in refs and drv_key in refs:
            for i in range(nh, n):
                c = dcol(i)
                rows[refs[opex_code] - 1][4 + i] = f"={c}{refs[drv_key]}*{c}{refs['REVT']}"
    sbc_drv = refs.get("driver_sbc_pct")
    if sbc_drv and "SBC" in refs:
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["SBC"] - 1][4 + i] = f"={c}{sbc_drv}*{c}{refs['OPEXT']}"
    tax_drv = refs.get("driver_tax_rate")
    if tax_drv:
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["TAX"] - 1][4 + i] = f"={c}{tax_drv}*{c}{refs['EBT']}"
    if "DA" in refs:
        for i in range(nh, n):
            rows[refs["DA"] - 1][4 + i] = f"={dcol(i-1)}{refs['DA']}"
    gws_write(sid, f"IS!A1:{col_range(n)}{len(rows)}", rows)
    return refs


def build_bs_sheet(sid, is_refs, periods, forecast_periods):
    all_p = periods + forecast_periods
    nh, n = len(periods), len(all_p)
    rows = [[], R("", "$m") + all_p, []]
    refs = {}
    def add(d): rows.append(d); return len(rows)
    def sf(col_i, row_num): return sumif_formula("", col_i).replace("{row}", str(row_num))
    def lf(row_num): return label_formula().replace("{row}", str(row_num))
    def hold(i, row): return f"={dcol(i-1)}{row}"

    for label, is_key in [("Revenue", "REVT"), ("COGS", "COGST"), ("OpEx", "OPEXT")]:
        d = R("", label)
        for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs[is_key]}")
        add(d)
    bs_rev, bs_cogs, bs_opex = len(rows)-2, len(rows)-1, len(rows)
    d = R("", "COGS + OpEx")
    for i in range(n): d.append(f"={dcol(i)}{bs_cogs}+{dcol(i)}{bs_opex}")
    bs_costs = add(d); add([])

    ca_components = []
    for code, label, ftype in BS_ASSETS:
        if code == "BS_TCA": continue
        row_num = len(rows) + 1
        d = R(code, lf(row_num) if ftype == "sumif_hold" else label)
        for i in range(n):
            if i < nh: d.append(sf(i, row_num))
            elif ftype == "cash": d.append(f"=CF!{dcol(i)}{{ending_cash}}")
            elif ftype == "days_rev": d.append(f"={dcol(i)}{bs_rev}/(365/{dcol(i)}{{dso}})")
            elif ftype == "days_cogs": d.append(f"=IF({dcol(i)}{{dio}}=0,0,{dcol(i)}{bs_cogs}/(365/{dcol(i)}{{dio}}))")
            else: d.append(hold(i, row_num) if i > 0 else "0")
        ca_components.append(add(d)); refs[code] = len(rows)
    d = R("BS_TCA", "Total Current Assets")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{cr}' for cr in ca_components)}")
    refs["BS_TCA"] = add(d); add([])

    nca_components = []
    for code, label, ftype in BS_NONCURRENT_ASSETS:
        if code == "BS_TNCA": continue
        row_num = len(rows) + 1
        d = R(code, lf(row_num) if ftype == "sumif_hold" else label)
        for i in range(n):
            if i < nh: d.append(sf(i, row_num))
            elif ftype == "ppe_rollforward": d.append("")
            else: d.append(hold(i, row_num) if i > 0 else "0")
        nca_components.append(add(d)); refs[code] = len(rows)
    d = R("BS_TNCA", "Total Non-Current Assets")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{cr}' for cr in nca_components)}")
    refs["BS_TNCA"] = add(d); add([])
    d = R("BS_TA", "Total Assets")
    for i in range(n): d.append(f"={dcol(i)}{refs['BS_TCA']}+{dcol(i)}{refs['BS_TNCA']}")
    refs["BS_TA"] = add(d); add([])

    add(["", "", "Metrics - days"])
    for drv_key, label, bs_code, base, default in [("dso", "Days Receivable (DSO)", "BS_AR", bs_rev, 60), ("dio", "Days Inventory (DIO)", "BS_INV", bs_cogs, 30), ("dpo", "Days Payable (DPO)", "BS_AP", bs_costs, 45)]:
        d = R("", label); item_row = refs.get(bs_code)
        for i in range(n): d.append(f"=IF({dcol(i)}{base}=0,\"\",365/({dcol(i)}{base}/{dcol(i)}{item_row}))" if i < nh and item_row else default)
        refs[drv_key] = add(d)
    add([]); add([])
    if "BS_AR" in refs:
        for i in range(nh, n): rows[refs["BS_AR"] - 1][4 + i] = f"={dcol(i)}{bs_rev}/(365/{dcol(i)}{refs['dso']})"
    if "BS_INV" in refs:
        for i in range(nh, n): rows[refs["BS_INV"] - 1][4 + i] = f"=IF({dcol(i)}{refs['dio']}=0,0,{dcol(i)}{bs_cogs}/(365/{dcol(i)}{refs['dio']}))"

    add(R("", "$m") + all_p); add([])
    capex_row_num = len(rows) + 1
    d = R("CF_CAPEX", "CapEx")
    for i in range(n):
        if i < nh: d.append(f'=ABS(IFERROR(SUMIF(Filing!$A:$A,$A{capex_row_num},Filing!{dcol(i)}:{dcol(i)}),""))')
        else: d.append(f"={dcol(i)}{{capex_pct}}*{dcol(i)}{bs_rev}")
    refs["capex"] = add(d)
    da_is, da_row_num = is_refs.get("DA"), len(rows) + 1
    d = R("DA", "D&A")
    for i in range(n): d.append(f"=IS!{dcol(i)}{da_is}" if da_is else (sf(i, da_row_num) if i < nh else "0"))
    refs["da"] = add(d)
    d = R("", "Net Increase to PP&E")
    for i in range(n): d.append(f"={dcol(i)}{refs['capex']}-{dcol(i)}{refs['da']}")
    add(d); add([])
    add(["", "", "Metrics - PP&E"])
    d = R("", "CapEx as % of Revenue")
    for i in range(n): d.append(f"={dcol(i)}{refs['capex']}/{dcol(i)}{bs_rev}" if i < nh else 0.05)
    refs["capex_pct"] = add(d)
    d = R("", "D&A as % of beg PP&E")
    for i in range(n):
        if i == 0: d.append("")
        elif i < nh: d.append(f"=IF({dcol(i-1)}{refs['BS_PPE']}=0,\"\",{dcol(i)}{refs['da']}/{dcol(i-1)}{refs['BS_PPE']})")
        else: d.append(0.15)
    refs["da_pct"] = add(d); add([]); add([])
    for i in range(nh, n):
        rows[refs["BS_PPE"] - 1][4 + i] = f"={dcol(i-1)}{refs['BS_PPE']}+{dcol(i)}{refs['capex']}-{dcol(i)}{refs['da']}"
        rows[refs["capex"] - 1][4 + i] = f"={dcol(i)}{refs['capex_pct']}*{dcol(i)}{bs_rev}"

    add(R("", "$m") + all_p); add([])
    cl_components = []
    for code, label, ftype in BS_CURRENT_LIABILITIES:
        if code == "BS_TCL": continue
        row_num = len(rows) + 1
        d = R(code, lf(row_num) if ftype == "sumif_hold" else label)
        for i in range(n):
            if i < nh: d.append(sf(i, row_num))
            elif ftype == "days_costs" and "dpo" in refs: d.append(f"={dcol(i)}{bs_costs}/(365/{dcol(i)}{refs['dpo']})")
            else: d.append(hold(i, row_num) if i > 0 else "0")
        cl_components.append(add(d)); refs[code] = len(rows)
    d = R("BS_TCL", "Total Current Liabilities")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{cr}' for cr in cl_components)}")
    refs["BS_TCL"] = add(d); add([])
    ncl_components = []
    for code, label, ftype in BS_NONCURRENT_LIABILITIES:
        if code == "BS_TNCL": continue
        row_num = len(rows) + 1
        d = R(code, lf(row_num) if ftype == "sumif_hold" else label)
        for i in range(n): d.append(sf(i, row_num) if i < nh else (hold(i, row_num) if i > 0 else "0"))
        ncl_components.append(add(d)); refs[code] = len(rows)
    d = R("BS_TNCL", "Total Non-Current Liabilities")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{cr}' for cr in ncl_components)}")
    refs["BS_TNCL"] = add(d); add([])
    d = R("BS_TL", "Total Liabilities")
    for i in range(n): d.append(f"={dcol(i)}{refs['BS_TCL']}+{dcol(i)}{refs['BS_TNCL']}")
    refs["BS_TL"] = add(d); add([]); add([])
    add(R("", "$m") + all_p); add([])
    cs_beg = add(R("", "Common Stock - beg") + [""] * n)
    d = R("", "(+) SBC"); sbc_is = is_refs.get("SBC")
    for i in range(n): d.append(f"=IS!{dcol(i)}{sbc_is}" if sbc_is else "0")
    sbc_eq, stpay_eq = add(d), add(R("", "(+/-) Stock Payments") + ["=CF!{c}{stpay}".replace("{c}", dcol(i)).replace("{stpay}", "0") for i in range(n)])
    cs_row_num = len(rows) + 1
    d = R("BS_CS", "Common Stock & APIC - end")
    for i in range(n): d.append(sf(i, cs_row_num) if i < nh else f"={dcol(i)}{cs_beg}+{dcol(i)}{sbc_eq}+{dcol(i)}{stpay_eq}")
    cs_end = add(d); refs["BS_CS"] = cs_end
    for i in range(1, n): rows[cs_beg - 1][4 + i] = f"={dcol(i-1)}{cs_end}"
    add([])
    row_num = len(rows) + 1
    d = R("BS_OE", "Other Equity (AOCI etc)")
    for i in range(n): d.append(sf(i, row_num) if i < nh else (hold(i, row_num) if i > 0 else "0"))
    oe_row = add(d); refs["BS_OE"] = oe_row; add([])
    re_beg = add(R("", "Retained Earnings - beg") + [""] * n)
    d = R("", "(+) Net Income")
    for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs['INC_NET']}")
    re_ni, re_buy, re_div = add(d), add(R("", "(-) Share Repurchases") + [0] * n), add(R("", "(-) Dividends") + [0] * n)
    re_row_num = len(rows) + 1
    d = R("BS_RE", "Retained Earnings - end")
    for i in range(n): d.append(sf(i, re_row_num) if i < nh else f"={dcol(i)}{re_beg}+{dcol(i)}{re_ni}+{dcol(i)}{re_buy}+{dcol(i)}{re_div}")
    re_end = add(d); refs["BS_RE"] = re_end
    for i in range(1, n): rows[re_beg - 1][4 + i] = f"={dcol(i-1)}{re_end}"
    add([])
    eq_components = [cs_end, re_end, oe_row]
    d = R("BS_TE", "Total Stockholders' Equity")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{cr}' for cr in eq_components)}")
    refs["BS_TE"] = add(d); add([])
    d = R("", "BALANCE CHECK: Assets - (Liabilities + Equity)")
    for i in range(n): d.append(f"={dcol(i)}{refs['BS_TA']}-{dcol(i)}{refs['BS_TL']}-{dcol(i)}{refs['BS_TE']}")
    add(d); refs["stpay_eq"], refs["re_buy"], refs["re_div"] = stpay_eq, re_buy, re_div
    gws_write(sid, f"BS!A1:{col_range(n)}{len(rows)}", rows)
    return refs


def build_cf_sheet(sid, is_refs, bs_refs, periods, forecast_periods):
    all_p = periods + forecast_periods
    nh, n = len(periods), len(all_p)
    rows = [[], R("", "$m") + all_p, []]
    refs = {}
    def add(d): rows.append(d); return len(rows)
    def sf(col_i, row_num): return sumif_formula("", col_i).replace("{row}", str(row_num))
    def lf(row_num): return label_formula().replace("{row}", str(row_num))
    def signed_sumif(col_i, row_num, sign):
        c = dcol(col_i)
        base = f'IFERROR(SUMIF(Filing!$A:$A,$A{row_num},Filing!{c}:{c}),"")'
        if sign == "-": return f"=-ABS({base})"
        elif sign == "+": return f"=ABS({base})"
        else: return f"={base}"

    d = R("CF_NI", "Net Income")
    for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs['INC_NET']}")
    ni = add(d); refs["CF_NI"] = ni; add([]); add(["", "", "Adjustments for non-cash items"])
    d = R("CF_DA", "  D&A")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['da']}")
    da = add(d); refs["CF_DA"] = da
    d = R("CF_SBC", "  SBC"); sbc_is = is_refs.get("SBC")
    for i in range(n): d.append(f"=IS!{dcol(i)}{sbc_is}" if sbc_is else "0")
    sbc = add(d); refs["CF_SBC"] = sbc
    op_other_rows = []
    for code, label in [("CF_OP1", "  Other Operating 1"), ("CF_OP2", "  Other Operating 2"), ("CF_OP3", "  Other Operating 3")]:
        row_num = len(rows) + 1; d = R(code, lf(row_num))
        for i in range(n): d.append(sf(i, row_num) if i < nh else 0)
        op_other_rows.append(add(d)); refs[code] = len(rows)
    d = R("", "  Subtotal Adjustments"); all_adj = [da, sbc] + op_other_rows
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{ar}' for ar in all_adj)}")
    sub = add(d); add([]); add(["", "", "Changes in working capital"])
    wc_items, cf_wc_codes = [], {"BS_AR": "CF_AR", "BS_INV": "CF_INV", "BS_AP": "CF_AP"}
    for bs_code, label, negate in [("BS_AR", "  Accounts Receivable", True), ("BS_INV", "  Inventories", True), ("BS_AP", "  Accounts Payable", False)]:
        bs_row = bs_refs.get(bs_code)
        if not bs_row: continue
        row_num = len(rows) + 1; d = R(cf_wc_codes[bs_code], label)
        for i in range(n):
            if i < nh: d.append(sf(i, row_num))
            else:
                sign = "-" if negate else ""
                d.append(f"={sign}(BS!{dcol(i)}{bs_row}-BS!{dcol(i-1)}{bs_row})")
        wc_items.append(add(d)); refs[cf_wc_codes[bs_code]] = len(rows)
    d = R("", "  Total WC Changes")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{wr}' for wr in wc_items)}" if wc_items else "0")
    wc_total = add(d); add([])
    d = R("CF_OPCF", "Net Cash from Operations")
    for i in range(n): d.append(f"={dcol(i)}{ni}+{dcol(i)}{sub}+{dcol(i)}{wc_total}")
    op = add(d); refs["CF_OPCF"] = op; add([]); add(["", "", "Investing Activities"])
    d = R("CF_CAPEX", "  Capital Expenditures")
    for i in range(n): d.append(f"=-BS!{dcol(i)}{bs_refs['capex']}")
    capex = add(d); refs["CF_CAPEX"] = capex
    inv_inputs = []
    for code, label, sign in [("CF_SECPUR", "  Purchases of Securities", "-"), ("CF_SECSAL", "  Sales/Maturities of Securities", "+"), ("CF_INV1", "  Other Investing", None)]:
        row_num = len(rows) + 1; d = R(code, lf(row_num))
        for i in range(n): d.append(signed_sumif(i, row_num, sign) if i < nh else 0)
        inv_inputs.append(add(d)); refs[code] = len(rows)
    d = R("CF_INVCF", "Net Cash from Investing"); all_inv = [capex] + inv_inputs
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{ir}' for ir in all_inv)}")
    inv = add(d); refs["CF_INVCF"] = inv; add([]); add(["", "", "Financing Activities"])
    fin_items = []
    for code, label, sign in [("CF_FIN1", "  Stock Payments", "-"), ("CF_BUY", "  Share Repurchases", "-"), ("CF_DIV", "  Dividends", "-"), ("CF_DISS", "  Debt Issuance", "+"), ("CF_DREP", "  Debt Repayment", "-"), ("CF_FIN2", "  Other Financing", None)]:
        row_num = len(rows) + 1; d = R(code, lf(row_num))
        for i in range(n): d.append(signed_sumif(i, row_num, sign) if i < nh else 0)
        fin_items.append(add(d)); refs[code] = len(rows)
    d = R("CF_FINCF", "Net Cash from Financing")
    for i in range(n): d.append(f"={'+'.join(f'{dcol(i)}{fr}' for fr in fin_items)}")
    fin = add(d); refs["CF_FINCF"] = fin; add([])
    fx_row_num = len(rows) + 1; d = R("CF_FX", "FX / Reconciliation")
    for i in range(n): d.append(sf(i, fx_row_num) if i < nh else 0)
    fx = add(d); refs["CF_FX"] = fx
    d = R("CF_NETCH", "Net Change in Cash")
    for i in range(n): d.append(f"={dcol(i)}{op}+{dcol(i)}{inv}+{dcol(i)}{fin}+{dcol(i)}{fx}")
    nch = add(d); refs["CF_NETCH"] = nch
    d = R("CF_BEGC", "Cash at Beginning"); beg_num = len(rows) + 1
    for i in range(n):
        if i < nh: d.append(signed_sumif(i, beg_num, "+"))
        elif i == nh: d.append(f"=BS!{dcol(i-1)}{bs_refs['BS_CASH']}")
        else: d.append(f"={dcol(i-1)}{beg_num + 1}")
    beg = add(d); refs["CF_BEGC"] = beg
    d = R("CF_ENDC", "Cash at End of Period")
    for i in range(n): d.append(f"={dcol(i)}{beg}+{dcol(i)}{nch}")
    end = add(d); refs["CF_ENDC"] = end; refs["ending_cash"] = end
    for i in range(nh + 1, n): rows[beg - 1][4 + i] = f"={dcol(i-1)}{end}"
    gws_write(sid, f"CF!A1:{col_range(n)}{len(rows)}", rows)
    return refs


def build_summary_sheet(sid, is_refs, bs_refs, cf_refs, periods, forecast_periods):
    all_p = periods + forecast_periods
    n = len(all_p)
    rows = [[], R("", "$m") + all_p, []]
    def add(d): rows.append(d); return len(rows)
    def is_link(key): return [f"=IS!{dcol(i)}{is_refs[key]}" for i in range(n)]
    s_rev = add(R("", "Revenue") + is_link("REVT")); add([])
    s_cogs = add(R("", "COGS") + is_link("COGST"))
    d = R("", "Gross Profit")
    for i in range(n): d.append(f"={dcol(i)}{s_rev}-{dcol(i)}{s_cogs}")
    s_gp = add(d); add([])
    s_opex = add(R("", "OpEx") + is_link("OPEXT")); add([])
    d = R("", "EBIT")
    for i in range(n): d.append(f"={dcol(i)}{s_gp}-{dcol(i)}{s_opex}")
    s_ebit = add(d); add([])
    other_row = add(R("", "Other Income") + is_link("INC_O"))
    d = R("", "EBT")
    for i in range(n): d.append(f"={dcol(i)}{s_ebit}+{dcol(i)}{other_row}")
    s_ebt = add(d); add([])
    s_tax = add(R("", "Tax") + is_link("TAX"))
    d = R("", "Net Income")
    for i in range(n): d.append(f"={dcol(i)}{s_ebt}-{dcol(i)}{s_tax}")
    s_ni = add(d); add([]); add([]); add(R("", "$m") + all_p); add([])
    d = R("", "Total Assets")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['BS_TA']}")
    s_ta = add(d)
    d = R("", "Total Liabilities")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['BS_TL']}")
    s_tl = add(d)
    d = R("", "Total Equity")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['BS_TE']}")
    s_te = add(d)
    d = R("", "Total L+E")
    for i in range(n): d.append(f"={dcol(i)}{s_tl}+{dcol(i)}{s_te}")
    s_tle = add(d); add([]); add([]); add(R("", "$m") + all_p); add([])
    d = R("", "Operating CF")
    for i in range(n): d.append(f"=CF!{dcol(i)}{cf_refs['CF_OPCF']}")
    s_opcf = add(d)
    d = R("", "Investing CF")
    for i in range(n): d.append(f"=CF!{dcol(i)}{cf_refs['CF_INVCF']}")
    s_invcf = add(d)
    d = R("", "Financing CF")
    for i in range(n): d.append(f"=CF!{dcol(i)}{cf_refs['CF_FINCF']}")
    s_fincf = add(d)
    d = R("", "Net Change in Cash")
    for i in range(n): d.append(f"=CF!{dcol(i)}{cf_refs['CF_NETCH']}")
    s_netch = add(d); add([]); add([]); add(["", "", "INVARIANT CHECKS (all must be 0)"]); add(R("", "") + all_p); add([])
    d = R("", "1. BS Balance (TA - TL - TE)")
    for i in range(n): d.append(f"={dcol(i)}{s_ta}-{dcol(i)}{s_tl}-{dcol(i)}{s_te}")
    add(d)
    ec = cf_refs.get("ending_cash")
    if ec:
        d = R("", "2. Cash (CF End - BS Cash)")
        for i in range(n): d.append(f"=IF(BS!{dcol(i)}{bs_refs['BS_CASH']}=0,0,CF!{dcol(i)}{ec}-BS!{dcol(i)}{bs_refs['BS_CASH']})")
        add(d)
    d = R("", "3. Net Income (IS - CF)")
    for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs['INC_NET']}-CF!{dcol(i)}{cf_refs['CF_NI']}")
    add(d)
    da_is, da_cf = is_refs.get("DA"), cf_refs.get("CF_DA")
    if da_is and da_cf:
        d = R("", "4. D&A (IS - CF)")
        for i in range(n): d.append(f"=IS!{dcol(i)}{da_is}-CF!{dcol(i)}{da_cf}")
        add(d)
    sbc_is, sbc_cf = is_refs.get("SBC"), cf_refs.get("CF_SBC")
    if sbc_is and sbc_cf:
        d = R("", "5. SBC (IS - CF)")
        for i in range(n): d.append(f"=IS!{dcol(i)}{sbc_is}-CF!{dcol(i)}{sbc_cf}")
        add(d)
    d = R("", "6. BS Assets (TCA + TNCA - TA)")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['BS_TCA']}+BS!{dcol(i)}{bs_refs['BS_TNCA']}-BS!{dcol(i)}{bs_refs['BS_TA']}")
    add(d)
    d = R("", "7. BS Liabilities (TCL + TNCL - TL)")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['BS_TCL']}+BS!{dcol(i)}{bs_refs['BS_TNCL']}-BS!{dcol(i)}{bs_refs['BS_TL']}")
    add(d)
    d = R("", "8. BS Equity (CS + RE + OE - TE)")
    for i in range(n): d.append(f"=BS!{dcol(i)}{bs_refs['BS_CS']}+BS!{dcol(i)}{bs_refs['BS_RE']}+BS!{dcol(i)}{bs_refs['BS_OE']}-BS!{dcol(i)}{bs_refs['BS_TE']}")
    add(d)
    cf_fx = cf_refs.get("CF_FX")
    d = R("", "9. CF Structure (Op+Inv+Fin+FX - NetCh)")
    for i in range(n):
        fx_term = f"+CF!{dcol(i)}{cf_fx}" if cf_fx else ""
        d.append(f"={dcol(i)}{s_opcf}+{dcol(i)}{s_invcf}+{dcol(i)}{s_fincf}{fx_term}-{dcol(i)}{s_netch}")
    add(d)
    beg = cf_refs.get("CF_BEGC")
    if beg and ec:
        d = R("", "10. Cash Proof (Beg + NetCh - End)")
        for i in range(n): d.append(f"=CF!{dcol(i)}{beg}+CF!{dcol(i)}{cf_refs['CF_NETCH']}-CF!{dcol(i)}{ec}")
        add(d)
    d = R("", "11. IS Gross Profit (Rev - COGS - GP)")
    for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs['REVT']}-IS!{dcol(i)}{is_refs['COGST']}-IS!{dcol(i)}{is_refs['GP']}")
    add(d)
    d = R("", "12. IS EBIT (GP - OpEx - OPINC)")
    for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs['GP']}-IS!{dcol(i)}{is_refs['OPEXT']}-IS!{dcol(i)}{is_refs['OPINC']}")
    add(d)
    d = R("", "13. IS Net Income (EBT - Tax - NI)")
    for i in range(n): d.append(f"=IS!{dcol(i)}{is_refs['EBT']}-IS!{dcol(i)}{is_refs['TAX']}-IS!{dcol(i)}{is_refs['INC_NET']}")
    add(d); add([])
    check_start, check_end = None, None
    for idx, row in enumerate(rows):
        if len(row) > 1 and isinstance(row[1], str):
            if row[1].startswith("1. BS Balance"): check_start = idx + 1
            if row[1].startswith("13. IS Net Income"): check_end = idx + 1
    if check_start and check_end:
        d = R("", "TOTAL ERRORS (must be 0)")
        for i in range(n): d.append(f"=SUMPRODUCT(({dcol(i)}{check_start}:{dcol(i)}{check_end}<>0)*1)")
        add(d)
    gws_write(sid, f"Summary!A1:{col_range(n)}{len(rows)}", rows)
    return {"check_start": check_start, "check_end": check_end, "total_rows": len(rows), "dollar_rows": [s_rev, s_cogs, s_gp, s_opex, s_ebit, other_row, s_ebt, s_tax, s_ni, s_ta, s_tl, s_te, s_tle, s_opcf, s_invcf, s_fincf, s_netch], "bold_rows": [s_rev, s_gp, s_ebit, s_ni, s_ta, s_tle, s_netch]}


def apply_formatting(sid, sheet_ids, is_refs, bs_refs, cf_refs, summary_info, periods, forecast_periods):
    nh, n = len(periods), len(periods) + len(forecast_periods)
    requests = []
    def _range(sheet_id, r1, r2, c1, c2): return {"sheetId": sheet_id, "startRowIndex": r1 - 1, "endRowIndex": r2, "startColumnIndex": c1, "endColumnIndex": c2}
    def bold_row(sheet_id, row, max_col=None): requests.append({"repeatCell": {"range": _range(sheet_id, row, row, 0, max_col or (n + 4)), "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}})
    def number_fmt(sheet_id, row, pattern, c1=4, c2=None): requests.append({"repeatCell": {"range": _range(sheet_id, row, row, c1, c2 or (n + 4)), "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}}, "fields": "userEnteredFormat.numberFormat"}})
    def blue_bg(sheet_id, row, c1, c2): requests.append({"repeatCell": {"range": _range(sheet_id, row, row, c1, c2), "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.85, "green": 0.92, "blue": 1.0}}}, "fields": "userEnteredFormat.backgroundColor"}})
    DOLLAR_FMT, PCT_FMT, DAYS_FMT = "#,##0", "0.0%", "#,##0.0"
    is_id, bs_id, cf_id, summ_id, filing_id = sheet_ids["IS"], sheet_ids["BS"], sheet_ids["CF"], sheet_ids["Summary"], sheet_ids["Filing"]
    fc_start, fc_end = 4 + nh, 4 + n
    for key in ["REVT", "COGST", "GP", "OPEXT", "OPINC", "EBT", "INC_NET"]:
        if key in is_refs: bold_row(is_id, is_refs[key])
    for key in ["REV1", "REV2", "REV3", "REVT", "COGS1", "COGS2", "COGS3", "COGST", "GP", "OPEX1", "OPEX2", "OPEX3", "OPEXT", "SBC", "OPINC", "INC_O", "EBT", "TAX", "INC_NET", "DA"]:
        if key in is_refs: number_fmt(is_id, is_refs[key], DOLLAR_FMT)
    for key in ["driver_rev_growth", "driver_cogs_pct", "driver_opex1_pct", "driver_opex2_pct", "driver_opex3_pct", "driver_sbc_pct", "driver_tax_rate", "margin_gp", "margin_ebit", "margin_ni"]:
        if key in is_refs: number_fmt(is_id, is_refs[key], PCT_FMT)
    for key in ["driver_rev_growth", "driver_cogs_pct", "driver_opex1_pct", "driver_opex2_pct", "driver_opex3_pct", "driver_sbc_pct", "driver_tax_rate"]:
        if key in is_refs: blue_bg(is_id, is_refs[key], fc_start, fc_end)
    for key in ["BS_TCA", "BS_TNCA", "BS_TA", "BS_TCL", "BS_TNCL", "BS_TL", "BS_TE"]:
        if key in bs_refs: bold_row(bs_id, bs_refs[key])
    for key in ["BS_CASH", "BS_AR", "BS_INV", "BS_CA1", "BS_CA2", "BS_CA3", "BS_TCA", "BS_PPE", "BS_LTA1", "BS_LTA2", "BS_TNCA", "BS_TA", "BS_AP", "BS_STD", "BS_OCL1", "BS_OCL2", "BS_TCL", "BS_LTD", "BS_NCL1", "BS_NCL2", "BS_TNCL", "BS_TL", "BS_CS", "BS_RE", "BS_OE", "BS_TE", "capex", "da"]:
        if key in bs_refs: number_fmt(bs_id, bs_refs[key], DOLLAR_FMT)
    for key in ["capex_pct", "da_pct"]:
        if key in bs_refs: number_fmt(bs_id, bs_refs[key], PCT_FMT)
    for key in ["dso", "dio", "dpo"]:
        if key in bs_refs: number_fmt(bs_id, bs_refs[key], DAYS_FMT)
    for key in ["dso", "dio", "dpo", "capex_pct", "da_pct", "BS_CA1", "BS_CA2", "BS_CA3", "BS_LTA1", "BS_LTA2", "BS_STD", "BS_OCL1", "BS_OCL2", "BS_LTD", "BS_NCL1", "BS_NCL2", "BS_OE"]:
        if key in bs_refs: blue_bg(bs_id, bs_refs[key], fc_start, fc_end)
    for key in ["CF_OPCF", "CF_INVCF", "CF_FINCF", "CF_NETCH", "CF_ENDC"]:
        if key in cf_refs: bold_row(cf_id, cf_refs[key])
    for key in ["CF_NI", "CF_DA", "CF_SBC", "CF_OP1", "CF_OP2", "CF_OP3", "CF_AR", "CF_INV", "CF_AP", "CF_OPCF", "CF_CAPEX", "CF_SECPUR", "CF_SECSAL", "CF_INV1", "CF_INVCF", "CF_FIN1", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP", "CF_FIN2", "CF_FINCF", "CF_NETCH", "CF_BEGC", "CF_ENDC"]:
        if key in cf_refs: number_fmt(cf_id, cf_refs[key], DOLLAR_FMT)
    for key in ["CF_OP1", "CF_OP2", "CF_OP3", "CF_SECPUR", "CF_SECSAL", "CF_INV1", "CF_FIN1", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP", "CF_FIN2"]:
        if key in cf_refs: blue_bg(cf_id, cf_refs[key], fc_start, fc_end)
    for row in summary_info.get("dollar_rows", []): number_fmt(summ_id, row, DOLLAR_FMT)
    for row in summary_info.get("bold_rows", []): bold_row(summ_id, row)
    check_start, check_end = summary_info.get("check_start"), summary_info.get("check_end")
    if check_start and check_end:
        for col_i in range(n): requests.append({"addConditionalFormatRule": {"rule": {"ranges": [_range(summ_id, check_start, check_end, 4 + col_i, 4 + col_i + 1)], "booleanRule": {"condition": {"type": "NUMBER_NOT_EQ", "values": [{"userEnteredValue": "0"}]}, "format": {"backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.8}}}}, "index": 0}})
    requests.append({"repeatCell": {"range": {"sheetId": filing_id, "startRowIndex": 2, "endRowIndex": 500, "startColumnIndex": 4, "endColumnIndex": 4 + n}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": DOLLAR_FMT}}}, "fields": "userEnteredFormat.numberFormat"}})
    if requests: gws_batch_update(sid, requests)


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"): print("Error: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr); sys.exit(1)
    if not shutil.which("gws"): print("Error: 'gws' CLI not found on PATH (required for Google Sheets access)", file=sys.stderr); sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument("--financials", required=True, nargs="+", help="One or more structured financials JSON files (newest first)")
    parser.add_argument("--company", default="Unknown")
    parser.add_argument("--skip-verify", action="store_true", help="Skip Python model verification (not recommended)")
    args = parser.parse_args()
    financials_list = []
    for path in args.financials:
        with open(path) as f: financials_list.append(json.load(f))
    financials = _merge_financials(financials_list)
    verified_items = None
    if not args.skip_verify:
        from pymodel import load_filing, compute_model, verify_model
        print("Pre-flight: verifying data with Python model...", file=sys.stderr)
        filing = load_filing(financials); m = compute_model(filing); errors = verify_model(m)
        if errors:
            print(f"\n*** {len(errors)} INVARIANT FAILURES — aborting ***", file=sys.stderr)
            for name, period, delta in errors: print(f"  {name}: {period} = {delta:,.0f}")
            sys.exit(1)
        verified_items = filing["items"]; all_p = m["all_periods"]
        for code in ["CF_FX", "CF_NETCH", "CF_ENDC", "CF_BEGC"]:
            if code in m["model"]:
                vals = {p: m["model"][code][i] for i, p in enumerate(all_p) if m["model"][code][i] != 0}
                if vals: verified_items[code] = {"label": m["labels"].get(code, code), "values": vals}
    if verified_items:
        periods = filing["periods"]; rows = [{"code": code, "label": info["label"], "values": info["values"]} for code, info in verified_items.items() if info["values"]]
        filing_data = {"periods": periods, "rows": rows}
    else: filing_data = classify_filing(financials)
    periods = filing_data["periods"]
    last_year = int(periods[-1][:4]); forecast_periods = [f"{last_year + i}E" for i in range(1, 6)]
    sid, url, sheet_ids = gws_create(f"{args.company} - 3-Statement Model", ["Filing", "IS", "BS", "CF", "Summary"])
    col_width_requests = []
    for s_name, s_id in sheet_ids.items(): col_width_requests.append({"updateDimensionProperties": {"range": {"sheetId": s_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2}, "properties": {"pixelSize": 50}, "fields": "pixelSize"}})
    gws_batch_update(sid, col_width_requests)
    row_sections = build_filing_sheet(sid, filing_data, periods)
    is_refs = build_is_sheet(sid, periods, forecast_periods)
    bs_refs = build_bs_sheet(sid, is_refs, periods, forecast_periods)
    cf_refs = build_cf_sheet(sid, is_refs, bs_refs, periods, forecast_periods)
    fix_cross_refs(sid, bs_refs, cf_refs, periods, forecast_periods)
    apply_filing_validation(sid, sheet_ids, row_sections)
    summary_info = build_summary_sheet(sid, is_refs, bs_refs, cf_refs, periods, forecast_periods)
    apply_formatting(sid, sheet_ids, is_refs, bs_refs, cf_refs, summary_info, periods, forecast_periods)
    print(json.dumps({"spreadsheet_id": sid, "url": url, "company": args.company, "periods": periods, "forecast_periods": forecast_periods}, indent=2))

if __name__ == "__main__": main()
