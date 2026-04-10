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

# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _run_gws(*args) -> dict:
    result = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"gws error: {result.stderr[:300]}", file=sys.stderr)
        raise RuntimeError("gws failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def gws_create(title, sheet_names):
    sheets = [{"properties": {"title": n, "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 4, "hideGridlines": True}}} for n in sheet_names]
    result = _run_gws("sheets", "spreadsheets", "create", "--json", json.dumps({
        "properties": {"title": title}, "sheets": sheets
    }))
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in result["sheets"]}
    return result["spreadsheetId"], result["spreadsheetUrl"], sheet_ids


def gws_write(sid, range_, values):
    params = json.dumps({"spreadsheetId": sid, "range": range_, "valueInputOption": "USER_ENTERED"})
    _run_gws("sheets", "spreadsheets", "values", "update", "--params", params, "--json", json.dumps({"values": values}))


def gws_batch_update(sid, requests):
    params = json.dumps({"spreadsheetId": sid})
    _run_gws("sheets", "spreadsheets", "batchUpdate", "--params", params, "--json", json.dumps({"requests": requests}))


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
# Master invariant: BS_TA = BS_TL + BS_TE
# If balance check ≠ 0, fix the filing classification — every item must be mapped.


# ---------------------------------------------------------------------------
# FILING DATA CLASSIFICATION — map company data to model codes
# ---------------------------------------------------------------------------

HAIKU = "claude-haiku-4-5-20251001"

BS_CODE_DEFS = {
    "BS_CASH": "Cash & Cash Equivalents",
    "BS_AR": "Accounts Receivable, net",
    "BS_INV": "Inventories",
    "BS_CA1": "Other Current Asset bucket 1 (catch-all: marketable securities, prepaid, deferred tax assets, vendor receivables, etc.)",
    "BS_CA2": "Other Current Asset bucket 2 (catch-all)",
    "BS_CA3": "Other Current Asset bucket 3 (catch-all)",
    "BS_TCA": "Total Current Assets — SUBTOTAL only",
    "BS_PPE": "Property, Plant & Equipment, net",
    "BS_LTA1": "Other Long-term Asset bucket 1 (catch-all: goodwill, intangibles, long-term investments, operating lease ROU, etc.)",
    "BS_LTA2": "Other Long-term Asset bucket 2 (catch-all)",
    "BS_TNCA": "Total Non-Current Assets — SUBTOTAL only",
    "BS_TA": "Total Assets — GRAND TOTAL only",
    "BS_AP": "Accounts Payable",
    "BS_STD": "Short-term Debt / Current portion of long-term debt / Commercial Paper",
    "BS_OCL1": "Other Current Liability bucket 1 (catch-all: deferred revenue, accrued expenses, etc.)",
    "BS_OCL2": "Other Current Liability bucket 2 (catch-all)",
    "BS_TCL": "Total Current Liabilities — SUBTOTAL only",
    "BS_LTD": "Long-term Debt (non-current term debt, bonds)",
    "BS_NCL1": "Other Non-Current Liability bucket 1 (catch-all: deferred tax liabilities, operating lease liabilities, etc.)",
    "BS_NCL2": "Other Non-Current Liability bucket 2 (catch-all)",
    "BS_TNCL": "Total Non-Current Liabilities — SUBTOTAL only",
    "BS_TL": "Total Liabilities — GRAND TOTAL only",
    "BS_CS": "Common Stock & Additional Paid-In Capital",
    "BS_RE": "Retained Earnings / Accumulated Deficit",
    "BS_OE": "Other Equity (AOCI, treasury stock, minority interest, etc.)",
    "BS_TE": "Total Stockholders' Equity — SUBTOTAL only",
    "SKIP": "Skip — redundant totals like total_liabilities_and_shareholders_equity",
}

CF_CODE_DEFS = {
    "CF_NI": "Net Income",
    "CF_DA": "Depreciation & Amortization",
    "CF_SBC": "Stock-Based Compensation",
    "CF_OP1": "Other non-cash operating adjustment (catch-all: deferred taxes, impairments, amortization of debt discount/securities, gains/losses, etc.)",
    "CF_OP2": "Other working capital / operating change — assets side (catch-all: other current and non-current asset changes)",
    "CF_OP3": "Other working capital / operating change — liabilities side (catch-all: other current and non-current liability changes)",
    "CF_AR": "Change in Accounts Receivable",
    "CF_INV": "Change in Inventories",
    "CF_AP": "Change in Accounts Payable",
    "CF_OPCF": "Net Cash from Operations — SECTION SUBTOTAL only",
    "CF_CAPEX": "Capital Expenditures (purchases of property, plant, equipment)",
    "CF_SECPUR": "Purchases of Marketable Securities",
    "CF_SECSAL": "Proceeds from Sales/Maturities of Marketable Securities",
    "CF_INV1": "Other Investing (catch-all: acquisitions, divestitures, other investing activities)",
    "CF_INVCF": "Net Cash from Investing — SECTION SUBTOTAL only",
    "CF_FIN1": "Stock-related payments (taxes on RSU vesting, net share settlement) and other misc financing",
    "CF_DIV": "Dividends Paid",
    "CF_BUY": "Share Repurchases / Stock Buybacks",
    "CF_DISS": "Debt Issuance / Proceeds from borrowing",
    "CF_DREP": "Debt Repayment",
    "CF_FIN2": "Other Financing (catch-all: commercial paper, other financing activities)",
    "CF_FINCF": "Net Cash from Financing — SECTION SUBTOTAL only",
    "CF_NETCH": "Net increase/decrease in cash — GRAND TOTAL only",
    "CF_BEGC": "Cash, cash equivalents (and restricted cash) at BEGINNING of period",
    "CF_ENDC": "Cash, cash equivalents (and restricted cash) at END of period",
}


def _flatten_bs(bs_data, periods):
    """Flatten nested per-period BS data into items with values across periods."""
    all_items = {}  # id -> {id, key, section, values}

    for period in periods:
        pdata = bs_data.get(period, {})

        def collect(data, section_path):
            for key, value in data.items():
                if isinstance(value, (int, float)):
                    item_id = f"{section_path}/{key}" if section_path else key
                    if item_id not in all_items:
                        all_items[item_id] = {"id": item_id, "key": key, "section": section_path, "values": {}}
                    all_items[item_id]["values"][period] = value
                elif isinstance(value, dict):
                    collect(value, f"{section_path}/{key}" if section_path else key)

        collect(pdata, "")

    return list(all_items.values())


def _flatten_cf(cf_data, periods):
    """Flatten nested CF JSON into list of {id, key, section, values}."""
    items = []

    op_cf = cf_data.get("operating_activities",
                        cf_data.get("cash_flows_from_operating_activities", {}))
    inv_cf = cf_data.get("investing_activities",
                         cf_data.get("cash_flows_from_investing_activities", {}))
    fin_cf = cf_data.get("financing_activities",
                         cf_data.get("cash_flows_from_financing_activities", {}))

    skip_top = {"operating_activities", "cash_flows_from_operating_activities",
                "investing_activities", "cash_flows_from_investing_activities",
                "financing_activities", "cash_flows_from_financing_activities",
                "unit", "currency_note"}

    def collect(section_data, section_name):
        for key, value in section_data.items():
            if not isinstance(value, dict):
                continue
            period_vals = {p: value[p] for p in periods
                          if p in value and isinstance(value[p], (int, float))}
            if period_vals:
                item_id = f"{section_name}/{key}"
                items.append({"id": item_id, "key": key, "section": section_name, "values": period_vals})
            else:
                collect(value, f"{section_name}/{key}")

    collect(op_cf, "operating")
    collect(inv_cf, "investing")
    collect(fin_cf, "financing")

    # Top-level items: beginning/ending balances, net change
    for top_key, value in cf_data.items():
        if top_key in skip_top or not isinstance(value, dict):
            continue
        period_vals = {p: value[p] for p in periods
                       if p in value and isinstance(value[p], (int, float))}
        if period_vals:
            item_id = f"summary/{top_key}"
            items.append({"id": item_id, "key": top_key, "section": "summary", "values": period_vals})
        else:
            collect(value, top_key)

    return items


def _clean_label(key):
    """Convert snake_case key to Title Case label."""
    return key.replace("_", " ").strip().title()


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

Return ONLY a JSON object mapping each item "id" to its assigned code. No explanation."""

    for attempt in range(2):
        response = client.messages.create(
            model=HAIKU,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
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
        # else: base already has this key, keep it (newer data wins)


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

    # Revenue: map up to 3 components, bucket extras into REV3
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

    # OpEx: map up to 3 components
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
        # Use pre-computed codes from structure_financials
        for item in coded_bs:
            add(item["code"], item["label"], item["values"])
    else:
        # Fallback: classify at build time
        bs_data = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
        bs_pkeys = [k for k in bs_data if isinstance(bs_data.get(k), dict) and k[:4].isdigit() and not k.lower().endswith("_usd")]
        if bs_pkeys:
            bs_periods = sorted(bs_pkeys)
        else:
            bs_data, bs_periods = _convert_section_first(bs_data)
            bs_periods = [p for p in bs_periods if not p.lower().endswith("_usd")]
        print("  Classifying BS items (no pre-computed codes)...", file=sys.stderr)
        bs_items = _flatten_bs(bs_data, bs_periods)
        if bs_items:
            bs_mapping = _llm_classify(bs_items, BS_CODE_DEFS, "Balance Sheet")
            for item in bs_items:
                code = bs_mapping.get(item["id"])
                if code and code != "SKIP":
                    add(code, _clean_label(item["key"]), item["values"])

    # --- CF ---
    coded_cf = cf_data.get("_coded_items")
    if coded_cf:
        # Use pre-computed codes from structure_financials
        for item in coded_cf:
            add(item["code"], item["label"], item["values"])
    else:
        # Fallback: classify at build time
        print("  Classifying CF items (no pre-computed codes)...", file=sys.stderr)
        cf_items = _flatten_cf(cf_data, periods)
        if cf_items:
            cf_mapping = _llm_classify(cf_items, CF_CODE_DEFS, "Cash Flow Statement")
            for item in cf_items:
                code = cf_mapping.get(item["id"])
                if code and code != "SKIP":
                    add(code, _clean_label(item["key"]), item["values"])

    return {"periods": periods, "rows": rows}


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------



def build_filing_sheet(sid, filing_data, periods, code_map=None):
    """Filing sheet: raw data with codes linked to IS/BS/CF via formulas.
    Returns row_sections: list of (row_index_0based, section) for data validation."""
    out = [[], R("", "Filing Data") + ["'" + p for p in periods], []]

    section_breaks = {"REVT": "INCOME STATEMENT", "BS_CASH": "BALANCE SHEET", "CF_NI": "CASH FLOWS"}
    seen = set()
    current_section = "IS"  # first rows before any break are IS
    row_sections = []  # (0-based row index, "IS"|"BS"|"CF")

    for item in filing_data["rows"]:
        sec = section_breaks.get(item["code"])
        if sec and sec not in seen:
            if sec == "BALANCE SHEET":
                current_section = "BS"
            elif sec == "CASH FLOWS":
                current_section = "CF"
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
    print(f"  Filing: {len(out)} rows", file=sys.stderr)
    return row_sections


def apply_filing_validation(sid, sheet_ids, row_sections):
    """Apply range-based dropdown validation to Filing!A cells.
    Each section block validates against its sheet's Col A (=IS!$A:$A, =BS!$A:$A, =CF!$A:$A)."""
    filing_sheet_id = sheet_ids["Filing"]
    is_sheet_id = sheet_ids["IS"]
    bs_sheet_id = sheet_ids["BS"]
    cf_sheet_id = sheet_ids["CF"]

    source_sheets = {"IS": is_sheet_id, "BS": bs_sheet_id, "CF": cf_sheet_id}

    # Group consecutive rows by section into contiguous ranges
    if not row_sections:
        return

    ranges = []  # (start_row_0based, end_row_0based_exclusive, section)
    cur_start, cur_section = row_sections[0]
    cur_end = cur_start + 1
    for row_idx, section in row_sections[1:]:
        if section == cur_section and row_idx <= cur_end + 2:
            # Allow small gaps (section headers/blanks) within same section
            cur_end = row_idx + 1
        else:
            ranges.append((cur_start, cur_end, cur_section))
            cur_start, cur_section = row_idx, section
            cur_end = row_idx + 1
    ranges.append((cur_start, cur_end, cur_section))

    requests = []
    for start, end, section in ranges:
        src_sheet_id = source_sheets[section]
        requests.append({
            "setDataValidation": {
                "range": {
                    "sheetId": filing_sheet_id,
                    "startRowIndex": start,
                    "endRowIndex": end,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue": f"={section}!$A:$A"}],
                    },
                    "showCustomUi": True,
                    "strict": True,
                },
            }
        })

    gws_batch_update(sid, requests)
    print(f"  Filing validation: {len(ranges)} section ranges (IS/BS/CF dropdowns)", file=sys.stderr)


def build_is_sheet(sid, periods, forecast_periods):
    """IS sheet. Fixed structure, SUMIF for historicals, formulas for forecasts."""
    all_p = periods + forecast_periods
    nh = len(periods)
    n = len(all_p)
    rows = []
    refs = {}  # code_or_key → row number

    def r():
        return len(rows)

    def add(data):
        rows.append(data)
        return r()

    add([])
    add(R("", "$m") + all_p)
    add([])

    # Build each IS row from IS_STRUCTURE
    # We'll track sum groups: REV1-3→REVT, COGS1-3→COGST, OPEX1-3→OPEXT
    sum_groups = {}  # total_code → [component row numbers]
    current_group = None

    for code, default_label, rtype in IS_STRUCTURE:
        if rtype == "blank":
            add([])
            continue
        if rtype == "label":
            add(["", "", default_label])
            continue

        row_num = r() + 1  # the row number this will occupy

        if rtype == "sumif":
            # Component row: SUMIF for historical, forecast depends on parent
            label_cell = label_formula().replace("{row}", str(row_num)) if default_label is None else default_label
            d = R(code, label_cell)
            for i in range(n):
                if i < nh:
                    d.append(sumif_formula(code, i).replace("{row}", str(row_num)))
                else:
                    d.append("")  # filled in after we know driver rows
            cur = add(d)
            refs[code] = cur
            # Track for sum groups
            if code.startswith("REV") and code != "REVT":
                sum_groups.setdefault("REVT", []).append(cur)
            elif code.startswith("COGS") and code != "COGST":
                sum_groups.setdefault("COGST", []).append(cur)
            elif code.startswith("OPEX") and code != "OPEXT":
                sum_groups.setdefault("OPEXT", []).append(cur)

        elif rtype == "sum":
            # Total row: always a SUM of its components
            d = R(code, default_label)
            component_rows = sum_groups.get(code, [])
            for i in range(n):
                c = dcol(i)
                if component_rows:
                    d.append(f"={'+'.join(f'{c}{cr}' for cr in component_rows)}")
                elif i < nh:
                    d.append(sumif_formula(code, i).replace("{row}", str(row_num)))
                else:
                    d.append("")
            cur = add(d)
            refs[code] = cur

        elif rtype == "formula":
            d = R(code, default_label)
            for i in range(n):
                c = dcol(i)
                if code == "GP":
                    d.append(f"={c}{refs['REVT']}-{c}{refs['COGST']}")
                elif code == "OPINC":
                    d.append(f"={c}{refs['GP']}-{c}{refs['OPEXT']}")
                elif code == "EBT":
                    d.append(f"={c}{refs['OPINC']}+{c}{refs['INC_O']}")
                elif code == "INC_NET":
                    d.append(f"={c}{refs['EBT']}-{c}{refs['TAX']}")
            cur = add(d)
            refs[code] = cur

        elif rtype.startswith("driver_"):
            d = R("", default_label)
            for i in range(n):
                c = dcol(i)
                if rtype == "driver_rev_growth":
                    if i == 0:
                        d.append("")
                    elif i < nh:
                        d.append(f"={c}{refs['REVT']}/{dcol(i-1)}{refs['REVT']}-1")
                    else:
                        d.append(0.05)
                elif rtype == "driver_cogs_pct":
                    if i < nh:
                        d.append(f"=IF({c}{refs['REVT']}=0,\"\",{c}{refs['COGST']}/{c}{refs['REVT']})")
                    else:
                        d.append(0.45)
                elif rtype.startswith("driver_opex") and rtype.endswith("_pct"):
                    slot = rtype.replace("driver_opex", "").replace("_pct", "")
                    opex_code = f"OPEX{slot}"
                    if opex_code in refs:
                        if i < nh:
                            d.append(f"=IF({c}{refs['REVT']}=0,\"\",{c}{refs[opex_code]}/{c}{refs['REVT']})")
                        else:
                            d.append(0.10)
                    else:
                        d.append("" if i < nh else 0)
                elif rtype == "driver_sbc_pct":
                    if i < nh:
                        d.append(f"=IF({c}{refs['OPEXT']}=0,\"\",{c}{refs['SBC']}/{c}{refs['OPEXT']})")
                    else:
                        d.append(0.20)
                elif rtype == "driver_tax_rate":
                    if i < nh:
                        d.append(f"=IF({c}{refs['EBT']}=0,\"\",{c}{refs['TAX']}/{c}{refs['EBT']})")
                    else:
                        d.append(0.21)
            cur = add(d)
            refs[rtype] = cur

        elif rtype.startswith("margin_"):
            d = R("", default_label)
            src_map = {"margin_gp": "GP", "margin_ebit": "OPINC", "margin_ni": "INC_NET"}
            src = refs.get(src_map.get(rtype, ""))
            for i in range(n):
                c = dcol(i)
                d.append(f"=IF({c}{refs['REVT']}=0,\"\",{c}{src}/{c}{refs['REVT']})" if src else "")
            refs[rtype] = add(d)

    # --- Fix forecast formulas ---
    # REVT forecast: prior * (1 + growth)
    growth_row = refs["driver_rev_growth"]
    for i in range(nh, n):
        c = dcol(i)
        rows[refs["REVT"] - 1][4 + i] = f"={dcol(i-1)}{refs['REVT']}*(1+{c}{growth_row})"

    # COGST forecast: cogs_pct * revenue (only if no components)
    cogs_pct_row = refs["driver_cogs_pct"]
    if not sum_groups.get("COGST"):
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["COGST"] - 1][4 + i] = f"={c}{cogs_pct_row}*{c}{refs['REVT']}"

    # OPEX component forecasts: opex_pct * revenue
    for slot in [1, 2, 3]:
        opex_code = f"OPEX{slot}"
        drv_key = f"driver_opex{slot}_pct"
        if opex_code in refs and drv_key in refs:
            for i in range(nh, n):
                c = dcol(i)
                rows[refs[opex_code] - 1][4 + i] = f"={c}{refs[drv_key]}*{c}{refs['REVT']}"

    # SBC forecast: sbc_pct * opex
    sbc_drv = refs.get("driver_sbc_pct")
    if sbc_drv and "SBC" in refs:
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["SBC"] - 1][4 + i] = f"={c}{sbc_drv}*{c}{refs['OPEXT']}"

    # TAX forecast: rate * EBT
    tax_drv = refs.get("driver_tax_rate")
    if tax_drv:
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["TAX"] - 1][4 + i] = f"={c}{tax_drv}*{c}{refs['EBT']}"

    # DA forecast: hold constant
    if "DA" in refs:
        for i in range(nh, n):
            rows[refs["DA"] - 1][4 + i] = f"={dcol(i-1)}{refs['DA']}"

    gws_write(sid, f"IS!A1:{col_range(n)}{len(rows)}", rows)
    print(f"  IS: {len(rows)} rows", file=sys.stderr)
    return refs


def build_bs_sheet(sid, is_refs, periods, forecast_periods):
    """BS sheet with sum invariants enforced."""
    all_p = periods + forecast_periods
    nh = len(periods)
    n = len(all_p)
    rows = []
    refs = {}

    def r():
        return len(rows)
    def add(data):
        rows.append(data)
        return r()

    def sf(col_i, row_num):
        """Externalized SUMIF: looks up code from $A of this row."""
        return sumif_formula("", col_i).replace("{row}", str(row_num))

    def lf(row_num):
        """Externalized label: INDEX/MATCH from Filing by $A code."""
        return label_formula().replace("{row}", str(row_num))

    def hold(i, row):
        return f"={dcol(i-1)}{row}"

    # === SECTION 1: WORKING CAPITAL ===
    add([])
    add(R("", "$m") + all_p)
    add([])

    # IS links for days calculations
    for label, is_key in [("Revenue", "REVT"), ("COGS", "COGST"), ("OpEx", "OPEXT")]:
        d = R("", label)
        for i in range(n):
            d.append(f"=IS!{dcol(i)}{is_refs[is_key]}")
        add(d)
    bs_rev, bs_cogs, bs_opex = r()-2, r()-1, r()

    d = R("", "COGS + OpEx")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{bs_cogs}+{c}{bs_opex}")
    bs_costs = add(d)
    add([])

    # Current Assets: BS_CASH, BS_AR, BS_INV, BS_CA1-3, BS_TCA
    ca_components = []
    for code, label, ftype in BS_ASSETS:
        if code == "BS_TCA":
            continue  # handle after components
        row_num = r() + 1
        dyn_label = lf(row_num) if ftype == "sumif_hold" else label
        d = R(code, dyn_label)
        for i in range(n):
            c = dcol(i)
            if i < nh:
                d.append(sf(i, row_num))
            elif ftype == "cash":
                d.append(f"=CF!{c}{{ending_cash}}")  # placeholder
            elif ftype == "days_rev":
                d.append(f"={c}{bs_rev}/(365/{c}{{dso}})")  # placeholder
            elif ftype == "days_cogs":
                d.append(f"=IF({c}{{dio}}=0,0,{c}{bs_cogs}/(365/{c}{{dio}}))")
            else:  # sumif_hold
                d.append(hold(i, row_num) if i > 0 else "0")
        cur = add(d)
        refs[code] = cur
        ca_components.append(cur)

    # BS_TCA = sum of all current asset components (INVARIANT)
    d = R("BS_TCA", "Total Current Assets")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{cr}' for cr in ca_components)}")
    refs["BS_TCA"] = add(d)
    add([])

    # Non-Current Assets: BS_PPE, BS_LTA1-2, BS_TNCA
    nca_components = []
    for code, label, ftype in BS_NONCURRENT_ASSETS:
        if code == "BS_TNCA":
            continue
        row_num = r() + 1
        dyn_label = lf(row_num) if ftype == "sumif_hold" else label
        d = R(code, dyn_label)
        for i in range(n):
            c = dcol(i)
            if i < nh:
                d.append(sf(i, row_num))
            elif ftype == "ppe_rollforward":
                d.append("")  # filled after capex/da rows exist
            else:
                d.append(hold(i, row_num) if i > 0 else "0")
        cur = add(d)
        refs[code] = cur
        nca_components.append(cur)

    d = R("BS_TNCA", "Total Non-Current Assets")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{cr}' for cr in nca_components)}")
    refs["BS_TNCA"] = add(d)
    add([])

    # BS_TA = BS_TCA + BS_TNCA (INVARIANT)
    d = R("BS_TA", "Total Assets")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{refs['BS_TCA']}+{c}{refs['BS_TNCA']}")
    refs["BS_TA"] = add(d)
    add([])

    # WC Drivers
    add(["", "", "Metrics - days"])
    driver_defs = [
        ("dso", "Days Receivable (DSO)", "BS_AR", bs_rev, 60),
        ("dio", "Days Inventory (DIO)", "BS_INV", bs_cogs, 30),
        ("dpo", "Days Payable (DPO)", "BS_AP", bs_costs, 45),
    ]
    for drv_key, label, bs_code, base, default in driver_defs:
        d = R("", label)
        item_row = refs.get(bs_code)
        for i in range(n):
            c = dcol(i)
            if i < nh and item_row:
                d.append(f"=IF({c}{base}=0,\"\",365/({c}{base}/{c}{item_row}))")
            else:
                d.append(default)
        refs[drv_key] = add(d)
    add([])
    add([])

    # Fix AR/INV forecast formulas
    if "BS_AR" in refs and "dso" in refs:
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["BS_AR"] - 1][4 + i] = f"={c}{bs_rev}/(365/{c}{refs['dso']})"
    if "BS_INV" in refs and "dio" in refs:
        for i in range(nh, n):
            c = dcol(i)
            rows[refs["BS_INV"] - 1][4 + i] = f"=IF({c}{refs['dio']}=0,0,{c}{bs_cogs}/(365/{c}{refs['dio']}))"

    # === PP&E DRIVERS ===
    add(R("", "$m") + all_p)
    add([])

    capex_row_num = r() + 1
    d = R("CF_CAPEX", "CapEx")
    for i in range(n):
        c = dcol(i)
        if i < nh:
            # ABS so it works whether filing stores capex as negative or positive
            col = dcol(i)
            d.append(f'=ABS(IFERROR(SUMIF(Filing!$A:$A,$A{capex_row_num},Filing!{col}:{col}),""))')
        else:
            d.append(f"={c}{{capex_pct}}*{c}{bs_rev}")
    refs["capex"] = add(d)

    da_is = is_refs.get("DA")
    da_row_num = r() + 1
    d = R("DA", "D&A")
    for i in range(n):
        c = dcol(i)
        d.append(f"=IS!{c}{da_is}" if da_is else (sf(i, da_row_num) if i < nh else "0"))
    refs["da"] = add(d)

    d = R("", "Net Increase to PP&E")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{refs['capex']}-{c}{refs['da']}")
    add(d)
    add([])

    add(["", "", "Metrics - PP&E"])
    d = R("", "CapEx as % of Revenue")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{refs['capex']}/{c}{bs_rev}" if i < nh else 0.05)
    refs["capex_pct"] = add(d)

    d = R("", "D&A as % of beg PP&E")
    for i in range(n):
        c = dcol(i)
        if i == 0:
            d.append("")
        elif i < nh:
            d.append(f"=IF({dcol(i-1)}{refs['BS_PPE']}=0,\"\",{c}{refs['da']}/{dcol(i-1)}{refs['BS_PPE']})")
        else:
            d.append(0.15)
    refs["da_pct"] = add(d)
    add([])
    add([])

    # Fix PPE forecast: ending = beg + capex - da
    for i in range(nh, n):
        c = dcol(i)
        rows[refs["BS_PPE"] - 1][4 + i] = f"={dcol(i-1)}{refs['BS_PPE']}+{c}{refs['capex']}-{c}{refs['da']}"
        rows[refs["capex"] - 1][4 + i] = f"={c}{refs['capex_pct']}*{c}{bs_rev}"

    # === CURRENT LIABILITIES ===
    add(R("", "$m") + all_p)
    add([])

    cl_components = []
    for code, label, ftype in BS_CURRENT_LIABILITIES:
        if code == "BS_TCL":
            continue
        row_num = r() + 1
        dyn_label = lf(row_num) if ftype == "sumif_hold" else label
        d = R(code, dyn_label)
        for i in range(n):
            c = dcol(i)
            if i < nh:
                d.append(sf(i, row_num))
            elif ftype == "days_costs" and "dpo" in refs:
                d.append(f"={c}{bs_costs}/(365/{c}{refs['dpo']})")
            else:
                d.append(hold(i, row_num) if i > 0 else "0")
        cur = add(d)
        refs[code] = cur
        cl_components.append(cur)

    d = R("BS_TCL", "Total Current Liabilities")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{cr}' for cr in cl_components)}")
    refs["BS_TCL"] = add(d)
    add([])

    # === NON-CURRENT LIABILITIES ===
    ncl_components = []
    for code, label, ftype in BS_NONCURRENT_LIABILITIES:
        if code == "BS_TNCL":
            continue
        row_num = r() + 1
        dyn_label = lf(row_num) if ftype == "sumif_hold" else label
        d = R(code, dyn_label)
        for i in range(n):
            d.append(sf(i, row_num) if i < nh else (hold(i, row_num) if i > 0 else "0"))
        cur = add(d)
        refs[code] = cur
        ncl_components.append(cur)

    d = R("BS_TNCL", "Total Non-Current Liabilities")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{cr}' for cr in ncl_components)}")
    refs["BS_TNCL"] = add(d)
    add([])

    # BS_TL = BS_TCL + BS_TNCL (INVARIANT)
    d = R("BS_TL", "Total Liabilities")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{refs['BS_TCL']}+{c}{refs['BS_TNCL']}")
    refs["BS_TL"] = add(d)
    add([])
    add([])

    # === EQUITY ROLL-FORWARD ===
    add(R("", "$m") + all_p)
    add([])

    # Common Stock
    cs_beg = add(R("", "Common Stock - beg") + [""] * n)
    d = R("", "(+) SBC")
    sbc_is = is_refs.get("SBC")
    for i in range(n):
        d.append(f"=IS!{dcol(i)}{sbc_is}" if sbc_is else "0")
    sbc_eq = add(d)
    stpay_eq = add(R("", "(+/-) Stock Payments") + ["=CF!{c}{stpay}".replace("{c}", dcol(i)).replace("{stpay}", "0") for i in range(n)])  # placeholder

    cs_row_num = r() + 1
    d = R("BS_CS", "Common Stock & APIC - end")
    for i in range(n):
        c = dcol(i)
        d.append(sf(i, cs_row_num) if i < nh else f"={c}{cs_beg}+{c}{sbc_eq}+{c}{stpay_eq}")
    cs_end = add(d)
    refs["BS_CS"] = cs_end
    for i in range(1, n):
        rows[cs_beg - 1][4 + i] = f"={dcol(i-1)}{cs_end}"
    add([])

    # Other Equity (AOCI + other)
    row_num = r() + 1
    d = R("BS_OE", "Other Equity (AOCI etc)")
    for i in range(n):
        d.append(sf(i, row_num) if i < nh else (hold(i, row_num) if i > 0 else "0"))
    oe_row = add(d)
    refs["BS_OE"] = oe_row
    add([])

    # Retained Earnings roll-forward
    re_beg = add(R("", "Retained Earnings - beg") + [""] * n)
    d = R("", "(+) Net Income")
    for i in range(n):
        d.append(f"=IS!{dcol(i)}{is_refs['INC_NET']}")
    re_ni = add(d)
    re_buy = add(R("", "(-) Share Repurchases") + [0] * n)  # placeholder
    re_div = add(R("", "(-) Dividends") + [0] * n)  # placeholder

    re_row_num = r() + 1
    d = R("BS_RE", "Retained Earnings - end")
    for i in range(n):
        c = dcol(i)
        d.append(sf(i, re_row_num) if i < nh else f"={c}{re_beg}+{c}{re_ni}+{c}{re_buy}+{c}{re_div}")
    re_end = add(d)
    refs["BS_RE"] = re_end
    for i in range(1, n):
        rows[re_beg - 1][4 + i] = f"={dcol(i-1)}{re_end}"
    add([])

    # BS_TE = CS + RE + OE (INVARIANT: sum of equity components)
    eq_components = [cs_end, re_end, oe_row]
    d = R("BS_TE", "Total Stockholders' Equity")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{cr}' for cr in eq_components)}")
    refs["BS_TE"] = add(d)
    add([])

    # MASTER INVARIANT: BS_TA = BS_TL + BS_TE
    # Balance check row
    d = R("", "BALANCE CHECK: Assets - (Liabilities + Equity)")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{refs['BS_TA']}-{c}{refs['BS_TL']}-{c}{refs['BS_TE']}")
    add(d)

    # Store extra refs for CF cross-linking
    refs["stpay_eq"] = stpay_eq
    refs["re_buy"] = re_buy
    refs["re_div"] = re_div

    gws_write(sid, f"BS!A1:{col_range(n)}{len(rows)}", rows)
    print(f"  BS: {len(rows)} rows", file=sys.stderr)
    return refs


def build_cf_sheet(sid, is_refs, bs_refs, periods, forecast_periods):
    """CF sheet fully linked to IS and BS."""
    all_p = periods + forecast_periods
    nh = len(periods)
    n = len(all_p)
    rows = []
    refs = {}

    def r():
        return len(rows)
    def add(data):
        rows.append(data)
        return r()

    def sf(col_i, row_num):
        """Externalized SUMIF: looks up code from $A of this row."""
        return sumif_formula("", col_i).replace("{row}", str(row_num))

    def lf(row_num):
        """Externalized label: INDEX/MATCH from Filing by $A code."""
        return label_formula().replace("{row}", str(row_num))

    def signed_sumif(col_i, row_num, sign):
        """SUMIF with sign enforcement. sign='-' forces negative, '+'=positive, None=raw."""
        col = dcol(col_i)
        base = f'IFERROR(SUMIF(Filing!$A:$A,$A{row_num},Filing!{col}:{col}),"")'
        if sign == "-":
            return f"=-ABS({base})"
        elif sign == "+":
            return f"=ABS({base})"
        else:
            return f"={base}"

    add([])
    add(R("", "$m") + all_p)
    add([])

    # Net Income from IS
    d = R("CF_NI", "Net Income")
    for i in range(n):
        d.append(f"=IS!{dcol(i)}{is_refs['INC_NET']}")
    ni = add(d)
    refs["CF_NI"] = ni

    add([])
    add(["", "", "Adjustments for non-cash items"])

    d = R("CF_DA", "  D&A")
    for i in range(n):
        d.append(f"=BS!{dcol(i)}{bs_refs['da']}")
    da = add(d)
    refs["CF_DA"] = da

    d = R("CF_SBC", "  SBC")
    sbc_is = is_refs.get("SBC")
    for i in range(n):
        d.append(f"=IS!{dcol(i)}{sbc_is}" if sbc_is else "0")
    sbc = add(d)
    refs["CF_SBC"] = sbc

    # Other operating adjustments (CF_OP1, CF_OP2, CF_OP3)
    op_other_rows = []
    for code, label in [("CF_OP1", "  Other Operating 1"),
                        ("CF_OP2", "  Other Operating 2"),
                        ("CF_OP3", "  Other Operating 3")]:
        row_num = r() + 1
        d = R(code, lf(row_num))
        for i in range(n):
            d.append(sf(i, row_num) if i < nh else 0)
        cur = add(d)
        op_other_rows.append(cur)
        refs[code] = cur

    d = R("", "  Subtotal Adjustments")
    all_adj = [da, sbc] + op_other_rows
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{ar}' for ar in all_adj)}")
    sub = add(d)

    add([])
    add(["", "", "Changes in working capital"])

    wc_items = []
    cf_wc_codes = {"BS_AR": "CF_AR", "BS_INV": "CF_INV", "BS_AP": "CF_AP"}
    for bs_code, label, negate in [("BS_AR", "  Accounts Receivable", True),
                                    ("BS_INV", "  Inventories", True),
                                    ("BS_AP", "  Accounts Payable", False)]:
        bs_row = bs_refs.get(bs_code)
        if not bs_row:
            continue
        cf_code = cf_wc_codes[bs_code]
        row_num = r() + 1
        d = R(cf_code, label)
        for i in range(n):
            c = dcol(i)
            if i < nh:
                d.append(sf(i, row_num))
            else:
                sign = "-" if negate else ""
                d.append(f"={sign}(BS!{c}{bs_row}-BS!{dcol(i-1)}{bs_row})")
        cur = add(d)
        wc_items.append(cur)
        refs[cf_code] = cur

    d = R("", "  Total WC Changes")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{wr}' for wr in wc_items)}" if wc_items else "0")
    wc_total = add(d)

    add([])

    # Net Operating CF
    d = R("CF_OPCF", "Net Cash from Operations")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{ni}+{c}{sub}+{c}{wc_total}")
    op = add(d)
    refs["CF_OPCF"] = op

    add([])
    add(["", "", "Investing Activities"])

    d = R("CF_CAPEX", "  Capital Expenditures")
    for i in range(n):
        d.append(f"=-BS!{dcol(i)}{bs_refs['capex']}")
    capex = add(d)
    refs["CF_CAPEX"] = capex

    # sign: "-" = always outflow, "+" = always inflow, None = keep raw
    inv_inputs = []
    for code, label, sign in [("CF_SECPUR", "  Purchases of Securities", "-"),
                               ("CF_SECSAL", "  Sales/Maturities of Securities", "+"),
                               ("CF_INV1", "  Other Investing", None)]:
        row_num = r() + 1
        d = R(code, lf(row_num))
        for i in range(n):
            if i < nh:
                d.append(signed_sumif(i, row_num, sign))
            else:
                d.append(0)
        cur = add(d)
        inv_inputs.append(cur)
        refs[code] = cur

    d = R("CF_INVCF", "Net Cash from Investing")
    all_inv = [capex] + inv_inputs
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{ir}' for ir in all_inv)}")
    inv = add(d)
    refs["CF_INVCF"] = inv

    add([])
    add(["", "", "Financing Activities"])

    fin_items = []
    for code, label, sign in [("CF_FIN1", "  Stock Payments", "-"),
                               ("CF_BUY", "  Share Repurchases", "-"),
                               ("CF_DIV", "  Dividends", "-"),
                               ("CF_DISS", "  Debt Issuance", "+"),
                               ("CF_DREP", "  Debt Repayment", "-"),
                               ("CF_FIN2", "  Other Financing", None)]:
        row_num = r() + 1
        d = R(code, lf(row_num))
        for i in range(n):
            if i < nh:
                d.append(signed_sumif(i, row_num, sign))
            else:
                d.append(0)
        cur = add(d)
        fin_items.append(cur)
        refs[code] = cur

    d = R("CF_FINCF", "Net Cash from Financing")
    for i in range(n):
        c = dcol(i)
        d.append(f"={'+'.join(f'{c}{fr}' for fr in fin_items)}")
    fin = add(d)
    refs["CF_FINCF"] = fin

    add([])

    fx_row_num = r() + 1
    d = R("CF_FX", "FX / Reconciliation")
    for i in range(n):
        if i < nh:
            d.append(sf(i, fx_row_num))
        else:
            d.append(0)
    fx = add(d)

    d = R("CF_NETCH", "Net Change in Cash")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{op}+{c}{inv}+{c}{fin}+{c}{fx}")
    nch = add(d)
    refs["CF_NETCH"] = nch

    d = R("CF_BEGC", "Cash at Beginning")
    beg_num = r() + 1
    for i in range(n):
        c = dcol(i)
        if i < nh:
            d.append(signed_sumif(i, beg_num, "+"))
        elif i == nh:
            d.append(f"=BS!{dcol(i-1)}{bs_refs['BS_CASH']}")
        else:
            d.append(f"={dcol(i-1)}{beg_num + 1}")
    beg = add(d)
    refs["CF_BEGC"] = beg

    d = R("CF_ENDC", "Cash at End of Period")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{beg}+{c}{nch}")
    end = add(d)
    refs["CF_ENDC"] = end
    refs["ending_cash"] = end

    for i in range(nh + 1, n):
        rows[beg - 1][4 + i] = f"={dcol(i-1)}{end}"

    gws_write(sid, f"CF!A1:{col_range(n)}{len(rows)}", rows)
    print(f"  CF: {len(rows)} rows", file=sys.stderr)
    return refs


def fix_cross_refs(sid, bs_refs, cf_refs, periods, forecast_periods):
    """Fix BS↔CF circular placeholders."""
    nh = len(periods)
    n = len(periods) + len(forecast_periods)

    # BS Cash → CF ending cash (forecast only)
    cash = bs_refs.get("BS_CASH")
    ec = cf_refs.get("ending_cash")
    if cash and ec:
        updates = [f"=CF!{dcol(i)}{ec}" for i in range(nh, n)]
        if updates:
            gws_write(sid, f"BS!{dcol(nh)}{cash}:{dcol(n-1)}{cash}", [updates])

    # BS equity: stock payments → CF
    stpay_cf = cf_refs.get("CF_FIN1")
    stpay_eq = bs_refs.get("stpay_eq")
    if stpay_cf and stpay_eq:
        updates = [f"=CF!{dcol(i)}{stpay_cf}" for i in range(n)]
        gws_write(sid, f"BS!{dcol(0)}{stpay_eq}:{dcol(n-1)}{stpay_eq}", [updates])

    # BS retained earnings: buybacks, dividends → CF
    buy_cf = cf_refs.get("CF_BUY")
    div_cf = cf_refs.get("CF_DIV")
    re_buy = bs_refs.get("re_buy")
    re_div = bs_refs.get("re_div")
    if buy_cf and re_buy:
        updates = [f"=CF!{dcol(i)}{buy_cf}" for i in range(n)]
        gws_write(sid, f"BS!{dcol(0)}{re_buy}:{dcol(n-1)}{re_buy}", [updates])
    if div_cf and re_div:
        updates = [f"=CF!{dcol(i)}{div_cf}" for i in range(n)]
        gws_write(sid, f"BS!{dcol(0)}{re_div}:{dcol(n-1)}{re_div}", [updates])


def build_summary_sheet(sid, is_refs, bs_refs, cf_refs, periods, forecast_periods):
    """Summary P&L, BS totals, and comprehensive invariant checks."""
    all_p = periods + forecast_periods
    n = len(all_p)
    rows = []
    def r():
        return len(rows)
    def add(data):
        rows.append(data)
        return r()

    def is_link(key):
        return [f"=IS!{dcol(i)}{is_refs[key]}" for i in range(n)]

    # ── P&L Summary ──
    add([])
    add(R("", "$m") + all_p)
    add([])

    s_rev = add(R("", "Revenue") + is_link("REVT"))
    add([])
    s_cogs = add(R("", "COGS") + is_link("COGST"))
    d = R("", "Gross Profit")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_rev}-{c}{s_cogs}")
    s_gp = add(d)
    add([])
    s_opex = add(R("", "OpEx") + is_link("OPEXT"))
    add([])
    d = R("", "EBIT")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_gp}-{c}{s_opex}")
    s_ebit = add(d)
    add([])
    other_row = add(R("", "Other Income") + is_link("INC_O"))
    d = R("", "EBT")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_ebit}+{c}{other_row}")
    s_ebt = add(d)
    add([])
    s_tax = add(R("", "Tax") + is_link("TAX"))
    d = R("", "Net Income")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_ebt}-{c}{s_tax}")
    s_ni = add(d)

    add([])
    add([])

    # ── BS Summary ──
    add(R("", "$m") + all_p)
    add([])
    d = R("", "Total Assets")
    for i in range(n):
        d.append(f"=BS!{dcol(i)}{bs_refs['BS_TA']}")
    s_ta = add(d)
    d = R("", "Total Liabilities")
    for i in range(n):
        d.append(f"=BS!{dcol(i)}{bs_refs['BS_TL']}")
    s_tl = add(d)
    d = R("", "Total Equity")
    for i in range(n):
        d.append(f"=BS!{dcol(i)}{bs_refs['BS_TE']}")
    s_te = add(d)
    d = R("", "Total L+E")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_tl}+{c}{s_te}")
    s_tle = add(d)

    add([])
    add([])

    # ── CF Summary ──
    add(R("", "$m") + all_p)
    add([])
    d = R("", "Operating CF")
    for i in range(n):
        d.append(f"=CF!{dcol(i)}{cf_refs['CF_OPCF']}")
    s_opcf = add(d)
    d = R("", "Investing CF")
    for i in range(n):
        d.append(f"=CF!{dcol(i)}{cf_refs['CF_INVCF']}")
    s_invcf = add(d)
    d = R("", "Financing CF")
    for i in range(n):
        d.append(f"=CF!{dcol(i)}{cf_refs['CF_FINCF']}")
    s_fincf = add(d)
    d = R("", "Net Change in Cash")
    for i in range(n):
        d.append(f"=CF!{dcol(i)}{cf_refs['CF_NETCH']}")
    s_netch = add(d)

    add([])
    add([])

    # ══════════════════════════════════════════════════════════════
    # INVARIANT CHECKS — all must be 0
    # ══════════════════════════════════════════════════════════════
    add(["", "", "INVARIANT CHECKS (all must be 0)"])
    add(R("", "") + all_p)
    add([])

    # 1. BS Balance: Assets = Liabilities + Equity
    d = R("", "1. BS Balance (TA - TL - TE)")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_ta}-{c}{s_tl}-{c}{s_te}")
    add(d)

    # 2. Cash: CF ending cash = BS cash
    ec = cf_refs.get("ending_cash")
    if ec:
        d = R("", "2. Cash (CF End - BS Cash)")
        for i in range(n):
            c = dcol(i)
            d.append(f"=CF!{c}{ec}-BS!{c}{bs_refs['BS_CASH']}")
        add(d)

    # 3. Net Income: IS = CF (cross-sheet linkage)
    d = R("", "3. Net Income (IS - CF)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=IS!{c}{is_refs['INC_NET']}-CF!{c}{cf_refs['CF_NI']}")
    add(d)

    # 4. D&A: IS = CF (cross-sheet)
    da_is = is_refs.get("DA")
    da_cf = cf_refs.get("CF_DA")
    if da_is and da_cf:
        d = R("", "4. D&A (IS - CF)")
        for i in range(n):
            c = dcol(i)
            d.append(f"=IS!{c}{da_is}-CF!{c}{da_cf}")
        add(d)

    # 5. SBC: IS = CF (cross-sheet)
    sbc_is = is_refs.get("SBC")
    sbc_cf = cf_refs.get("CF_SBC")
    if sbc_is and sbc_cf:
        d = R("", "5. SBC (IS - CF)")
        for i in range(n):
            c = dcol(i)
            d.append(f"=IS!{c}{sbc_is}-CF!{c}{sbc_cf}")
        add(d)

    # 6. BS Assets: TCA + TNCA = TA
    d = R("", "6. BS Assets (TCA + TNCA - TA)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=BS!{c}{bs_refs['BS_TCA']}+BS!{c}{bs_refs['BS_TNCA']}-BS!{c}{bs_refs['BS_TA']}")
    add(d)

    # 7. BS Liabilities: TCL + TNCL = TL
    d = R("", "7. BS Liabilities (TCL + TNCL - TL)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=BS!{c}{bs_refs['BS_TCL']}+BS!{c}{bs_refs['BS_TNCL']}-BS!{c}{bs_refs['BS_TL']}")
    add(d)

    # 8. BS Equity: CS + RE + OE = TE
    d = R("", "8. BS Equity (CS + RE + OE - TE)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=BS!{c}{bs_refs['BS_CS']}+BS!{c}{bs_refs['BS_RE']}+BS!{c}{bs_refs['BS_OE']}-BS!{c}{bs_refs['BS_TE']}")
    add(d)

    # 9. CF Structure: OpCF + InvCF + FinCF + FX = Net Change
    d = R("", "9. CF Structure (Op+Inv+Fin - NetCh)")
    for i in range(n):
        c = dcol(i)
        d.append(f"={c}{s_opcf}+{c}{s_invcf}+{c}{s_fincf}-{c}{s_netch}")
    add(d)

    # 10. CF Cash Proof: Beg + Net Change = End
    beg = cf_refs.get("CF_BEGC")
    if beg and ec:
        d = R("", "10. Cash Proof (Beg + NetCh - End)")
        for i in range(n):
            c = dcol(i)
            d.append(f"=CF!{c}{beg}+CF!{c}{cf_refs['CF_NETCH']}-CF!{c}{ec}")
        add(d)

    # 11. IS: GP = Rev - COGS
    d = R("", "11. IS Gross Profit (Rev - COGS - GP)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=IS!{c}{is_refs['REVT']}-IS!{c}{is_refs['COGST']}-IS!{c}{is_refs['GP']}")
    add(d)

    # 12. IS: EBIT = GP - OpEx
    d = R("", "12. IS EBIT (GP - OpEx - OPINC)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=IS!{c}{is_refs['GP']}-IS!{c}{is_refs['OPEXT']}-IS!{c}{is_refs['OPINC']}")
    add(d)

    # 13. IS: NI = EBT - Tax
    d = R("", "13. IS Net Income (EBT - Tax - NI)")
    for i in range(n):
        c = dcol(i)
        d.append(f"=IS!{c}{is_refs['EBT']}-IS!{c}{is_refs['TAX']}-IS!{c}{is_refs['INC_NET']}")
    add(d)

    add([])

    # Roll-up: count of non-zero checks
    check_start = None
    check_end = None
    for idx, row in enumerate(rows):
        if len(row) > 1 and isinstance(row[1], str):
            if row[1].startswith("1. BS Balance"):
                check_start = idx + 1  # 1-based
            if row[1].startswith("13. IS Net Income"):
                check_end = idx + 1
    if check_start and check_end:
        d = R("", "TOTAL ERRORS (must be 0)")
        for i in range(n):
            c = dcol(i)
            d.append(f"=SUMPRODUCT(({c}{check_start}:{c}{check_end}<>0)*1)")
        add(d)

    gws_write(sid, f"Summary!A1:{col_range(n)}{len(rows)}", rows)
    print(f"  Summary: {len(rows)} rows", file=sys.stderr)
    return {
        "check_start": check_start, "check_end": check_end, "total_rows": len(rows),
        "dollar_rows": [s_rev, s_cogs, s_gp, s_opex, s_ebit, other_row, s_ebt, s_tax, s_ni,
                        s_ta, s_tl, s_te, s_tle, s_opcf, s_invcf, s_fincf, s_netch],
        "bold_rows": [s_rev, s_gp, s_ebit, s_ni, s_ta, s_tle, s_netch],
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def apply_formatting(sid, sheet_ids, is_refs, bs_refs, cf_refs, summary_info, periods, forecast_periods):
    """Apply formatting: bold totals, blue inputs, percent/number formats, red invariant errors."""
    nh = len(periods)
    n = len(periods) + len(forecast_periods)
    requests = []

    # Helpers
    def _range(sheet_id, r1, r2, c1, c2):
        """GridRange dict. r1/r2 are 1-based row numbers, converted to 0-based."""
        return {"sheetId": sheet_id, "startRowIndex": r1 - 1, "endRowIndex": r2,
                "startColumnIndex": c1, "endColumnIndex": c2}

    def bold_row(sheet_id, row, max_col=None):
        mc = max_col or (n + 4)
        requests.append({"repeatCell": {
            "range": _range(sheet_id, row, row, 0, mc),
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }})

    def number_fmt(sheet_id, row, pattern, c1=4, c2=None):
        c2 = c2 or (n + 4)
        requests.append({"repeatCell": {
            "range": _range(sheet_id, row, row, c1, c2),
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat",
        }})

    def blue_bg(sheet_id, row, c1, c2):
        requests.append({"repeatCell": {
            "range": _range(sheet_id, row, row, c1, c2),
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.85, "green": 0.92, "blue": 1.0}}},
            "fields": "userEnteredFormat.backgroundColor",
        }})

    DOLLAR_FMT = "#,##0"
    PCT_FMT = "0.0%"
    DAYS_FMT = "#,##0.0"

    is_id = sheet_ids["IS"]
    bs_id = sheet_ids["BS"]
    cf_id = sheet_ids["CF"]
    summ_id = sheet_ids["Summary"]
    filing_id = sheet_ids["Filing"]

    # Forecast column range (0-based): columns 4+nh through 4+n
    fc_start = 4 + nh
    fc_end = 4 + n

    # ── IS Formatting ──
    # Bold totals and formulas
    is_bold = ["REVT", "COGST", "GP", "OPEXT", "OPINC", "EBT", "INC_NET"]
    for key in is_bold:
        if key in is_refs:
            bold_row(is_id, is_refs[key])

    # Number format for $ rows
    is_dollar = ["REV1", "REV2", "REV3", "REVT", "COGS1", "COGS2", "COGS3", "COGST",
                 "GP", "OPEX1", "OPEX2", "OPEX3", "OPEXT", "SBC", "OPINC", "INC_O",
                 "EBT", "TAX", "INC_NET", "DA"]
    for key in is_dollar:
        if key in is_refs:
            number_fmt(is_id, is_refs[key], DOLLAR_FMT)

    # Percent format for driver/margin rows
    is_pct = ["driver_rev_growth", "driver_cogs_pct", "driver_opex1_pct", "driver_opex2_pct",
              "driver_opex3_pct", "driver_sbc_pct", "driver_tax_rate",
              "margin_gp", "margin_ebit", "margin_ni"]
    for key in is_pct:
        if key in is_refs:
            number_fmt(is_id, is_refs[key], PCT_FMT)

    # Blue background for INPUT driver cells in forecast columns
    is_input_drivers = ["driver_rev_growth", "driver_cogs_pct", "driver_opex1_pct",
                        "driver_opex2_pct", "driver_opex3_pct", "driver_sbc_pct", "driver_tax_rate"]
    for key in is_input_drivers:
        if key in is_refs:
            blue_bg(is_id, is_refs[key], fc_start, fc_end)

    # ── BS Formatting ──
    # Bold totals
    bs_bold = ["BS_TCA", "BS_TNCA", "BS_TA", "BS_TCL", "BS_TNCL", "BS_TL", "BS_TE"]
    for key in bs_bold:
        if key in bs_refs:
            bold_row(bs_id, bs_refs[key])

    # Number format for $ rows
    bs_dollar = ["BS_CASH", "BS_AR", "BS_INV", "BS_CA1", "BS_CA2", "BS_CA3", "BS_TCA",
                 "BS_PPE", "BS_LTA1", "BS_LTA2", "BS_TNCA", "BS_TA",
                 "BS_AP", "BS_STD", "BS_OCL1", "BS_OCL2", "BS_TCL",
                 "BS_LTD", "BS_NCL1", "BS_NCL2", "BS_TNCL", "BS_TL",
                 "BS_CS", "BS_RE", "BS_OE", "BS_TE",
                 "capex", "da"]
    for key in bs_dollar:
        if key in bs_refs:
            number_fmt(bs_id, bs_refs[key], DOLLAR_FMT)

    # Percent format for BS drivers
    bs_pct = ["capex_pct", "da_pct"]
    for key in bs_pct:
        if key in bs_refs:
            number_fmt(bs_id, bs_refs[key], PCT_FMT)

    # Days format
    bs_days = ["dso", "dio", "dpo"]
    for key in bs_days:
        if key in bs_refs:
            number_fmt(bs_id, bs_refs[key], DAYS_FMT)

    # Blue INPUT cells for BS forecast drivers
    bs_input_drivers = ["dso", "dio", "dpo", "capex_pct", "da_pct"]
    for key in bs_input_drivers:
        if key in bs_refs:
            blue_bg(bs_id, bs_refs[key], fc_start, fc_end)

    # Blue for hold-constant items in forecast
    bs_hold = ["BS_CA1", "BS_CA2", "BS_CA3", "BS_LTA1", "BS_LTA2",
               "BS_STD", "BS_OCL1", "BS_OCL2", "BS_LTD", "BS_NCL1", "BS_NCL2", "BS_OE"]
    for key in bs_hold:
        if key in bs_refs:
            blue_bg(bs_id, bs_refs[key], fc_start, fc_end)

    # ── CF Formatting ──
    # Bold totals
    cf_bold = ["CF_OPCF", "CF_INVCF", "CF_FINCF", "CF_NETCH", "CF_ENDC"]
    for key in cf_bold:
        if key in cf_refs:
            bold_row(cf_id, cf_refs[key])

    # Number format for all CF $ rows
    cf_dollar = ["CF_NI", "CF_DA", "CF_SBC", "CF_OP1", "CF_OP2", "CF_OP3",
                 "CF_AR", "CF_INV", "CF_AP", "CF_OPCF",
                 "CF_CAPEX", "CF_SECPUR", "CF_SECSAL", "CF_INV1", "CF_INVCF",
                 "CF_FIN1", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP", "CF_FIN2", "CF_FINCF",
                 "CF_NETCH", "CF_BEGC", "CF_ENDC"]
    for key in cf_dollar:
        if key in cf_refs:
            number_fmt(cf_id, cf_refs[key], DOLLAR_FMT)

    # Blue for CF forecast input items
    cf_input = ["CF_OP1", "CF_OP2", "CF_OP3", "CF_SECPUR", "CF_SECSAL", "CF_INV1",
                "CF_FIN1", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP", "CF_FIN2"]
    for key in cf_input:
        if key in cf_refs:
            blue_bg(cf_id, cf_refs[key], fc_start, fc_end)

    # ── Summary Formatting ──
    # Dollar format for data rows only
    for row in summary_info.get("dollar_rows", []):
        number_fmt(summ_id, row, DOLLAR_FMT)

    # Bold for key totals
    for row in summary_info.get("bold_rows", []):
        bold_row(summ_id, row)

    # Conditional formatting: red background if invariant check != 0
    check_start = summary_info.get("check_start")
    check_end = summary_info.get("check_end")
    if check_start and check_end:
        for col_i in range(n):
            c = dcol(col_i)
            requests.append({"addConditionalFormatRule": {
                "rule": {
                    "ranges": [_range(summ_id, check_start, check_end, 4 + col_i, 4 + col_i + 1)],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_NOT_EQ", "values": [{"userEnteredValue": "0"}]},
                        "format": {"backgroundColor": {"red": 1.0, "green": 0.8, "blue": 0.8}},
                    },
                },
                "index": 0,
            }})

    # ── Filing: number format for all data columns (skip header rows) ──
    requests.append({"repeatCell": {
        "range": {"sheetId": filing_id, "startRowIndex": 2, "endRowIndex": 500,
                  "startColumnIndex": 4, "endColumnIndex": 4 + n},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": DOLLAR_FMT}}},
        "fields": "userEnteredFormat.numberFormat",
    }})

    if requests:
        gws_batch_update(sid, requests)
    print(f"  Formatting: {len(requests)} requests", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)
    if not shutil.which("gws"):
        print("Error: 'gws' CLI not found on PATH (required for Google Sheets access)", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--financials", required=True, nargs="+",
                        help="One or more structured financials JSON files (newest first)")
    parser.add_argument("--company", default="Unknown")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip Python model verification (not recommended)")
    args = parser.parse_args()

    financials_list = []
    for path in args.financials:
        with open(path) as f:
            financials_list.append(json.load(f))
    print(f"Loaded {len(financials_list)} financials file(s)", file=sys.stderr)

    financials = _merge_financials(financials_list)

    # --- Pre-flight: verify data with Python model ---
    verified_items = None
    if not args.skip_verify:
        from pymodel import load_filing, compute_model, verify_model
        print("Pre-flight: verifying data with Python model...", file=sys.stderr)
        filing = load_filing(financials)
        m = compute_model(filing)
        errors = verify_model(m)
        if errors:
            print(f"\n*** {len(errors)} INVARIANT FAILURES — aborting ***", file=sys.stderr)
            for name, period, delta in errors:
                print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
            print("\nFix data or use --skip-verify to override.", file=sys.stderr)
            sys.exit(1)
        print("  All invariants pass — proceeding to sheet build.", file=sys.stderr)
        # Transfer computed values (CF_FX, reconciled CF_NETCH/ENDC/BEGC) back to items
        verified_items = filing["items"]
        all_p = m["all_periods"]
        for code in ["CF_FX", "CF_NETCH", "CF_ENDC", "CF_BEGC"]:
            if code in m["model"]:
                vals = {}
                for i, p in enumerate(all_p):
                    v = m["model"][code][i]
                    if v != 0:
                        vals[p] = v
                if vals:
                    verified_items[code] = {"label": m["labels"].get(code, code), "values": vals}

    print("Classifying filing data...", file=sys.stderr)
    if verified_items:
        # Use the verified items directly — no re-classification needed
        periods = filing["periods"]
        rows = []
        for code, info in verified_items.items():
            if info["values"]:
                rows.append({"code": code, "label": info["label"], "values": info["values"]})
        filing_data = {"periods": periods, "rows": rows}
    else:
        filing_data = classify_filing(financials)
    periods = filing_data["periods"]
    codes = [r["code"] for r in filing_data["rows"]]
    print(f"  {len(periods)} periods, {len(codes)} rows: {codes}", file=sys.stderr)

    last_year = int(periods[-1][:4])
    forecast_periods = [f"{last_year + i}E" for i in range(1, 6)]

    title = f"{args.company} - 3-Statement Model"
    print(f"\nCreating: {title}", file=sys.stderr)
    sid, url, sheet_ids = gws_create(title, ["Filing", "IS", "BS", "CF", "Summary"])
    print(f"  URL: {url}", file=sys.stderr)

    # Set columns A and B to width 50px on all sheets
    col_width_requests = []
    for sheet_name, sheet_id in sheet_ids.items():
        col_width_requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 2,
                },
                "properties": {"pixelSize": 50},
                "fields": "pixelSize",
            }
        })
    gws_batch_update(sid, col_width_requests)

    print("\n1. Filing...", file=sys.stderr)
    row_sections = build_filing_sheet(sid, filing_data, periods)

    print("2. IS...", file=sys.stderr)
    is_refs = build_is_sheet(sid, periods, forecast_periods)

    print("3. BS...", file=sys.stderr)
    bs_refs = build_bs_sheet(sid, is_refs, periods, forecast_periods)

    print("4. CF...", file=sys.stderr)
    cf_refs = build_cf_sheet(sid, is_refs, bs_refs, periods, forecast_periods)

    print("5. Cross-refs...", file=sys.stderr)
    fix_cross_refs(sid, bs_refs, cf_refs, periods, forecast_periods)

    print("6. Filing validation...", file=sys.stderr)
    apply_filing_validation(sid, sheet_ids, row_sections)

    print("7. Summary...", file=sys.stderr)
    summary_info = build_summary_sheet(sid, is_refs, bs_refs, cf_refs, periods, forecast_periods)

    print("8. Formatting...", file=sys.stderr)
    apply_formatting(sid, sheet_ids, is_refs, bs_refs, cf_refs, summary_info, periods, forecast_periods)

    print(f"\nDone! {url}", file=sys.stderr)
    print(json.dumps({"spreadsheet_id": sid, "url": url, "company": args.company,
                       "periods": periods, "forecast_periods": forecast_periods}, indent=2))


if __name__ == "__main__":
    main()
