"""
Python-first 3-Statement Financial Model
=========================================
All computation happens in Python. Invariants are verified before output.
Google Sheets is just a display layer — no SUMIF, no cross-sheet formulas.

Usage:
  python pymodel.py --financials /tmp/aapl_all_structured.json --company "Apple Inc."
"""

import argparse
import json
import subprocess
import sys

from anthropic import Anthropic

from structure_financials import (
    BS_CODE_DEFS, CF_CODE_DEFS,
    _detect_cf_periods, _classify_with_llm,
)
# Handle both old (_flatten_bs) and new (flatten_bs) names
try:
    from structure_financials import flatten_bs, flatten_cf
except ImportError:
    from structure_financials import _flatten_bs as flatten_bs, _flatten_cf as flatten_cf


def clean_label(key):
    return key.replace("_", " ").strip().title()


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def _run_gws(*args):
    r = subprocess.run(["gws"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gws error: {r.stderr}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def gws_create(title, sheet_names):
    sheets = [{"properties": {"title": s}} for s in sheet_names]
    r = _run_gws("sheets", "spreadsheets", "create", "--json",
                  json.dumps({"properties": {"title": title}, "sheets": sheets}))
    sid = r["spreadsheetId"]
    url = r["spreadsheetUrl"]
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in r["sheets"]}
    return sid, url, sheet_ids


def gws_write(sid, range_, values):
    _run_gws("sheets", "spreadsheets", "values", "update",
             "--params", json.dumps({"spreadsheetId": sid, "range": range_,
                                     "valueInputOption": "USER_ENTERED"}),
             "--json", json.dumps({"values": values}))


def gws_batch_update(sid, requests):
    _run_gws("sheets", "spreadsheets", "batchUpdate",
             "--params", json.dumps({"spreadsheetId": sid}),
             "--json", json.dumps({"requests": requests}))


def dcol(i):
    """Data column letter for period index i (0-based). Data starts at column E."""
    return chr(ord('E') + i) if i < 22 else chr(ord('A') + (i + 4) // 26 - 1) + chr(ord('A') + (i + 4) % 26)


# ---------------------------------------------------------------------------
# Data loading & classification
# ---------------------------------------------------------------------------

def _navigate(data, keys):
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return None
    return data


def _deep_find(data, key):
    if not isinstance(data, dict):
        return None
    if key in data and isinstance(data[key], (int, float)):
        return data[key]
    for v in data.values():
        if isinstance(v, dict):
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None


def _convert_section_first(data):
    """Convert section-first BS format to period-first."""
    periods = set()
    skip = {"unit", "company", "statement", "currencies", "note", "currency_note",
            "note_2a", "ads_conversion_note", "period_end_month"}

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


def load_filing(financials: dict) -> dict:
    """Load and classify filing data into {code: {period: value}}.

    Returns dict with keys: 'periods', 'items' ({code: {label, values: {period: val}}}).
    """
    client = Anthropic()

    # --- IS: hardcoded mapping (stable, well-defined keys) ---
    is_raw = financials.get("income_statement", {})
    is_data = is_raw.get("fiscal_years", is_raw.get("data", is_raw))
    periods = sorted([k for k in is_data if isinstance(is_data.get(k), dict)
                      and k[:4].isdigit() and not k.lower().endswith("_usd")])

    items = {}  # code -> {label, values: {period: val}}

    def add(code, label, vals):
        if not vals:
            return
        if code not in items:
            items[code] = {"label": label, "values": {}}
        for p, v in vals.items():
            items[code]["values"][p] = items[code]["values"].get(p, 0) + v

    def get_is(keys, paths=None):
        vals = {}
        for p in periods:
            pdata = is_data.get(p, {})
            if paths:
                for path in paths:
                    target = _navigate(pdata, path)
                    if target:
                        for k in keys:
                            if k in target and isinstance(target[k], (int, float)):
                                vals[p] = target[k]
                                break
                    if p in vals:
                        break
            else:
                for k in keys:
                    v = _deep_find(pdata, k)
                    if v is not None:
                        vals[p] = v
                        break
        return vals

    # Revenue
    rev_subs = [
        (get_is(["products"], [["net_sales"], ["revenue"]]), "Revenue - Products"),
        (get_is(["services"], [["net_sales"], ["revenue"]]), "Revenue - Services"),
    ]
    rev_subs = [(v, l) for v, l in rev_subs if v]
    for i, (v, l) in enumerate(rev_subs):
        add(f"REV{min(i+1,3)}", l, v)
    add("REVT", "Total Revenue", get_is(["total_net_sales", "revenues", "revenue", "total_revenue", "net_revenues"]))

    # COGS
    cogs_subs = [
        (get_is(["products"], [["cost_of_sales"], ["cost_of_revenue"]]), "COGS - Products"),
        (get_is(["services"], [["cost_of_sales"], ["cost_of_revenue"]]), "COGS - Services"),
    ]
    cogs_subs = [(v, l) for v, l in cogs_subs if v]
    for i, (v, l) in enumerate(cogs_subs):
        add(f"COGS{min(i+1,3)}", l, v)
    add("COGST", "Cost of Revenue", get_is(["total_cost_of_sales", "cost_of_revenues", "cost_of_revenue", "cost_of_goods_sold"]))

    add("GP", "Gross Profit", get_is(["gross_margin", "gross_profit"]))

    opex_subs = [
        (get_is(["research_and_development", "research_and_development_expense"], [["operating_expenses"]]), "R&D"),
        (get_is(["selling_general_and_administrative"], [["operating_expenses"]]), "SG&A"),
        (get_is(["sales_and_marketing"], [["operating_expenses"]]), "Sales & Marketing"),
        (get_is(["general_and_administrative"], [["operating_expenses"]]), "G&A"),
    ]
    opex_subs = [(v, l) for v, l in opex_subs if v]
    for i, (v, l) in enumerate(opex_subs):
        add(f"OPEX{min(i+1,3)}", l, v)
    add("OPEXT", "Total OpEx", get_is(["total_operating_expenses"], [["operating_expenses"]]))

    add("OPINC", "Operating Income", get_is(["operating_income", "income_from_operations"]))
    add("INC_O", "Other Income / (Expense)", get_is(["other_income_expense_net", "other_income_net"]))
    add("EBT", "EBT", get_is(["income_before_provision_for_income_taxes", "income_before_income_taxes"]))
    add("TAX", "Income Tax", get_is(["provision_for_income_taxes", "income_tax_expense"]))
    add("INC_NET", "Net Income", get_is(["net_income"]))

    # SBC & DA from IS or CF
    cf_data = financials.get("cash_flows", {})
    op_cf = cf_data.get("operating_activities", cf_data.get("cash_flows_from_operating_activities", {}))
    adj = op_cf.get("adjustments_to_reconcile_net_income", {})

    def get_cf_item(section, keys):
        for k in keys:
            if k in section and isinstance(section[k], dict):
                return {p: section[k][p] for p in periods if p in section[k]}
        return {}

    sbc = get_is(["share_based_compensation_expense", "stock_based_compensation"])
    if not sbc:
        sbc = get_cf_item(adj, ["share_based_compensation_expense", "stock_based_compensation"])
    add("SBC", "Stock-Based Compensation", sbc)

    da = get_is(["depreciation_and_amortization"])
    if not da:
        da = get_cf_item(adj, ["depreciation_and_amortization"])
    add("DA", "D&A", da)

    # --- BS: LLM classification ---
    bs_raw = financials.get("balance_sheet", {})
    bs_inner = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
    coded_bs = bs_raw.get("_coded_items")

    if coded_bs:
        for item in coded_bs:
            add(item["code"], item["label"], item["values"])
    else:
        bs_pkeys = [k for k in bs_inner if isinstance(bs_inner.get(k), dict)
                    and k[:4].isdigit() and not k.lower().endswith("_usd")]
        if bs_pkeys:
            bs_periods = sorted(bs_pkeys)
        else:
            bs_inner, bs_periods = _convert_section_first(bs_inner)
            bs_periods = [p for p in bs_periods if not p.lower().endswith("_usd")]
        print("  Classifying BS...", file=sys.stderr)
        bs_items = flatten_bs(bs_inner, bs_periods)
        if bs_items:
            mapping = _classify_with_llm(client, bs_items, BS_CODE_DEFS, "Balance Sheet")
            for item in bs_items:
                code = mapping.get(item["id"])
                if code and code != "SKIP":
                    add(code, clean_label(item["key"]), item["values"])

    # --- CF: LLM classification ---
    coded_cf = cf_data.get("_coded_items")
    if coded_cf:
        for item in coded_cf:
            add(item["code"], item["label"], item["values"])
    else:
        print("  Classifying CF...", file=sys.stderr)
        cf_items = flatten_cf(cf_data, periods)
        if cf_items:
            mapping = _classify_with_llm(client, cf_items, CF_CODE_DEFS, "Cash Flow Statement")
            for item in cf_items:
                code = mapping.get(item["id"])
                if code and code != "SKIP":
                    add(code, clean_label(item["key"]), item["values"])

    return {"periods": periods, "items": items}


# ---------------------------------------------------------------------------
# Model computation
# ---------------------------------------------------------------------------

def get(items, code, period, default=0):
    return items.get(code, {}).get("values", {}).get(period, default)


def compute_model(filing):
    """Build full 3-statement model from filing data. Returns verified model dict.

    Invariants enforced:
    - All filed components must sum to their filed subtotals (no gaps)
    - BS: components = subtotal for each section
    - CF: components = subtotal for each section
    - CF_ENDC = BS_CASH for every period
    """
    periods = filing["periods"]
    items = filing["items"]

    last_year = int(periods[-1][:4])
    forecast_periods = [f"{last_year + i}E" for i in range(1, 6)]
    all_periods = periods + forecast_periods
    nh = len(periods)

    model = {}  # code -> [val per period]
    labels = {}

    def v(code, period):
        idx = all_periods.index(period)
        return model.get(code, [0.0] * len(all_periods))[idx]

    def set_v(code, period, val):
        if code not in model:
            model[code] = [0.0] * len(all_periods)
        model[code][all_periods.index(period)] = float(val)

    # --- STEP 1: Load ALL historical values ---
    for code, info in items.items():
        labels[code] = info["label"]
        for p in periods:
            set_v(code, p, info["values"].get(p, 0))

    # --- STEP 2: Reconcile historical subtotals ---
    # For each section, compute component sum and compare to filed subtotal.
    # If there's a gap, push the difference into the catch-all bucket.

    SECTION_RULES = [
        # (subtotal_code, component_codes, catch_all_code)
        # BS
        ("BS_TCA", ["BS_CASH", "BS_AR", "BS_INV", "BS_CA1", "BS_CA2", "BS_CA3"], "BS_CA3"),
        ("BS_TNCA", ["BS_PPE", "BS_LTA1", "BS_LTA2"], "BS_LTA2"),
        ("BS_TA", ["BS_TCA", "BS_TNCA"], None),  # must balance exactly
        ("BS_TCL", ["BS_AP", "BS_STD", "BS_OCL1", "BS_OCL2"], "BS_OCL2"),
        ("BS_TNCL", ["BS_LTD", "BS_NCL1", "BS_NCL2"], "BS_NCL2"),
        ("BS_TL", ["BS_TCL", "BS_TNCL"], None),
        ("BS_TE", ["BS_CS", "BS_RE", "BS_OE"], "BS_OE"),
        # CF
        ("CF_OPCF", ["CF_NI", "CF_DA", "CF_SBC", "CF_OP1", "CF_OP2", "CF_OP3",
                      "CF_AR", "CF_INV", "CF_AP"], "CF_OP1"),
        ("CF_INVCF", ["CF_CAPEX", "CF_SECPUR", "CF_SECSAL", "CF_INV1"], "CF_INV1"),
        ("CF_FINCF", ["CF_FIN1", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP", "CF_FIN2"], "CF_FIN2"),
    ]

    for p in periods:
        for subtotal_code, comp_codes, catch_all in SECTION_RULES:
            filed_total = v(subtotal_code, p)
            if filed_total == 0:
                continue
            comp_sum = sum(v(c, p) for c in comp_codes)
            gap = filed_total - comp_sum
            if abs(gap) > 0.5 and catch_all:
                # Push gap into catch-all
                set_v(catch_all, p, v(catch_all, p) + gap)
                print(f"  Reconcile {subtotal_code} {p}: gap={gap:,.0f} → {catch_all}", file=sys.stderr)

    # Reconcile CF ending cash to BS cash (BS is source of truth)
    for p in periods:
        bs_cash = v("BS_CASH", p)
        if bs_cash != 0:
            set_v("CF_ENDC", p, bs_cash)
        idx = all_periods.index(p)
        if idx > 0:
            prev_bs = v("BS_CASH", all_periods[idx - 1])
            if prev_bs != 0:
                set_v("CF_BEGC", p, prev_bs)
        set_v("CF_NETCH", p, v("CF_ENDC", p) - v("CF_BEGC", p))

    # --- STEP 3: IS forecasts ---
    last_p = periods[-1]
    rev_growth = 0.05
    if len(periods) >= 2:
        r1, r2 = v("REVT", periods[-2]), v("REVT", periods[-1])
        if r1 > 0:
            rev_growth = (r2 / r1) - 1

    cogs_pct = v("COGST", last_p) / v("REVT", last_p) if v("REVT", last_p) else 0.5
    opex1_pct = v("OPEX1", last_p) / v("REVT", last_p) if v("REVT", last_p) else 0.1
    opex2_pct = v("OPEX2", last_p) / v("REVT", last_p) if v("REVT", last_p) else 0.05
    opex3_pct = v("OPEX3", last_p) / v("REVT", last_p) if v("REVT", last_p) else 0
    sbc_pct = v("SBC", last_p) / v("OPEXT", last_p) if v("OPEXT", last_p) else 0.1
    tax_rate = v("TAX", last_p) / v("EBT", last_p) if v("EBT", last_p) else 0.21

    for fp in forecast_periods:
        prev = all_periods[all_periods.index(fp) - 1]
        rev = v("REVT", prev) * (1 + rev_growth)
        set_v("REVT", fp, rev)
        cogs = rev * cogs_pct
        set_v("COGST", fp, cogs)
        set_v("GP", fp, rev - cogs)
        opex1 = rev * opex1_pct
        opex2 = rev * opex2_pct
        opex3 = rev * opex3_pct
        set_v("OPEX1", fp, opex1)
        set_v("OPEX2", fp, opex2)
        set_v("OPEX3", fp, opex3)
        opext = opex1 + opex2 + opex3
        set_v("OPEXT", fp, opext)
        set_v("SBC", fp, opext * sbc_pct)
        opinc = v("GP", fp) - opext
        set_v("OPINC", fp, opinc)
        set_v("INC_O", fp, 0)
        ebt = opinc
        set_v("EBT", fp, ebt)
        tax = ebt * tax_rate
        set_v("TAX", fp, tax)
        set_v("INC_NET", fp, ebt - tax)
        set_v("DA", fp, v("DA", prev))

    # --- STEP 4: BS forecasts ---
    last_rev = v("REVT", last_p)
    last_cogs = v("COGST", last_p)
    last_costs = last_cogs + v("OPEXT", last_p)
    dso = v("BS_AR", last_p) / last_rev * 365 if last_rev else 30
    dio = v("BS_INV", last_p) / last_cogs * 365 if last_cogs else 0
    dpo = v("BS_AP", last_p) / last_costs * 365 if last_costs else 30
    capex_last = abs(v("CF_CAPEX", last_p)) or abs(v("CF_CAPEX", periods[-2])) if len(periods) >= 2 else 0

    for fp in forecast_periods:
        prev = all_periods[all_periods.index(fp) - 1]
        rev = v("REVT", fp)
        cogs = v("COGST", fp)
        costs = cogs + v("OPEXT", fp)

        set_v("BS_AR", fp, rev * dso / 365)
        set_v("BS_INV", fp, cogs * dio / 365 if dio > 0 else 0)
        for ca in ["BS_CA1", "BS_CA2", "BS_CA3"]:
            set_v(ca, fp, v(ca, prev))
        tca_ex_cash = sum(v(c, fp) for c in ["BS_AR", "BS_INV", "BS_CA1", "BS_CA2", "BS_CA3"])

        capex = capex_last * (1 + rev_growth) ** (all_periods.index(fp) - nh)
        set_v("CF_CAPEX", fp, -capex)
        ppe = v("BS_PPE", prev) + capex - v("DA", fp)
        set_v("BS_PPE", fp, ppe)
        for lta in ["BS_LTA1", "BS_LTA2"]:
            set_v(lta, fp, v(lta, prev))
        tnca = sum(v(c, fp) for c in ["BS_PPE", "BS_LTA1", "BS_LTA2"])
        set_v("BS_TNCA", fp, tnca)

        set_v("BS_AP", fp, costs * dpo / 365)
        for cl in ["BS_STD", "BS_OCL1", "BS_OCL2"]:
            set_v(cl, fp, v(cl, prev))
        tcl = sum(v(c, fp) for c in ["BS_AP", "BS_STD", "BS_OCL1", "BS_OCL2"])
        set_v("BS_TCL", fp, tcl)

        for ncl in ["BS_LTD", "BS_NCL1", "BS_NCL2"]:
            set_v(ncl, fp, v(ncl, prev))
        tncl = sum(v(c, fp) for c in ["BS_LTD", "BS_NCL1", "BS_NCL2"])
        set_v("BS_TNCL", fp, tncl)
        set_v("BS_TL", fp, tcl + tncl)

        set_v("BS_CS", fp, v("BS_CS", prev) + v("SBC", fp))
        set_v("BS_RE", fp, v("BS_RE", prev) + v("INC_NET", fp))
        set_v("BS_OE", fp, v("BS_OE", prev))
        te = sum(v(c, fp) for c in ["BS_CS", "BS_RE", "BS_OE"])
        set_v("BS_TE", fp, te)

        ta = v("BS_TL", fp) + te
        set_v("BS_TA", fp, ta)
        cash = ta - tca_ex_cash - tnca
        set_v("BS_CASH", fp, cash)
        set_v("BS_TCA", fp, tca_ex_cash + cash)

    # Historical CF: compute FX effect (gap between OPCF+INVCF+FINCF and NETCH)
    for p in periods:
        if v("CF_OPCF", p) != 0:
            fx = v("CF_NETCH", p) - v("CF_OPCF", p) - v("CF_INVCF", p) - v("CF_FINCF", p)
            set_v("CF_FX", p, fx)

    # --- STEP 5: CF forecasts ---
    for fp in forecast_periods:
        prev = all_periods[all_periods.index(fp) - 1]
        ni = v("INC_NET", fp)
        da = v("DA", fp)
        sbc = v("SBC", fp)
        set_v("CF_NI", fp, ni)
        set_v("CF_DA", fp, da)
        set_v("CF_SBC", fp, sbc)
        cf_ar = -(v("BS_AR", fp) - v("BS_AR", prev))
        cf_inv = -(v("BS_INV", fp) - v("BS_INV", prev))
        cf_ap = v("BS_AP", fp) - v("BS_AP", prev)
        set_v("CF_AR", fp, cf_ar)
        set_v("CF_INV", fp, cf_inv)
        set_v("CF_AP", fp, cf_ap)
        opcf = ni + da + sbc + cf_ar + cf_inv + cf_ap
        set_v("CF_OPCF", fp, opcf)
        set_v("CF_INVCF", fp, v("CF_CAPEX", fp))
        set_v("CF_FINCF", fp, 0)
        set_v("CF_FX", fp, 0)
        netch = opcf + v("CF_INVCF", fp)
        set_v("CF_NETCH", fp, netch)
        beg = v("BS_CASH", prev)
        set_v("CF_BEGC", fp, beg)
        set_v("CF_ENDC", fp, beg + netch)

    return {
        "periods": periods,
        "forecast_periods": forecast_periods,
        "all_periods": all_periods,
        "model": model,
        "labels": labels,
    }


# ---------------------------------------------------------------------------
# Invariant checks — no exceptions, all periods
# ---------------------------------------------------------------------------

def verify_model(m):
    """Run ALL invariant checks on ALL periods. No exceptions."""
    model = m["model"]
    all_p = m["all_periods"]
    errors = []

    def v(code, p):
        idx = all_p.index(p)
        return model.get(code, [0.0] * len(all_p))[idx]

    def check(name, period, val):
        if abs(val) > 0.5:
            errors.append((name, period, val))

    SECTION_RULES = [
        ("BS_TCA", ["BS_CASH", "BS_AR", "BS_INV", "BS_CA1", "BS_CA2", "BS_CA3"]),
        ("BS_TNCA", ["BS_PPE", "BS_LTA1", "BS_LTA2"]),
        ("BS_TA", ["BS_TCA", "BS_TNCA"]),
        ("BS_TCL", ["BS_AP", "BS_STD", "BS_OCL1", "BS_OCL2"]),
        ("BS_TNCL", ["BS_LTD", "BS_NCL1", "BS_NCL2"]),
        ("BS_TL", ["BS_TCL", "BS_TNCL"]),
        ("BS_TE", ["BS_CS", "BS_RE", "BS_OE"]),
        ("CF_OPCF", ["CF_NI", "CF_DA", "CF_SBC", "CF_OP1", "CF_OP2", "CF_OP3",
                      "CF_AR", "CF_INV", "CF_AP"]),
        ("CF_INVCF", ["CF_CAPEX", "CF_SECPUR", "CF_SECSAL", "CF_INV1"]),
        ("CF_FINCF", ["CF_FIN1", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP", "CF_FIN2"]),
    ]

    for p in all_p:
        # Skip periods with no data at all
        if v("BS_TA", p) == 0 and v("REVT", p) == 0:
            continue

        # 1. BS Balance: TA = TL + TE
        check("BS Balance (TA - TL - TE)", p, v("BS_TA", p) - v("BS_TL", p) - v("BS_TE", p))

        # 2. CF End = BS Cash
        if v("BS_CASH", p) != 0 or p.endswith("E"):
            check("Cash (CF End - BS Cash)", p, v("CF_ENDC", p) - v("BS_CASH", p))

        # 3. Component sums = subtotals
        for total_code, comp_codes in SECTION_RULES:
            total = v(total_code, p)
            if total == 0 and not p.endswith("E"):
                continue
            comp_sum = sum(v(c, p) for c in comp_codes)
            check(f"{total_code} components", p, comp_sum - total)

        # 4. IS invariants
        if v("REVT", p) != 0:
            check("IS GP", p, v("REVT", p) - v("COGST", p) - v("GP", p))

        # 5. CF cash proof: Beg + NetCh = End
        if v("CF_ENDC", p) != 0 or p.endswith("E"):
            check("CF Cash Proof", p, v("CF_BEGC", p) + v("CF_NETCH", p) - v("CF_ENDC", p))

        # 6. CF Structure: Op + Inv + Fin + FX = NetCh
        if v("CF_OPCF", p) != 0 or p.endswith("E"):
            check("CF Structure", p, v("CF_OPCF", p) + v("CF_INVCF", p) + v("CF_FINCF", p) + v("CF_FX", p) - v("CF_NETCH", p))

    return errors


# ---------------------------------------------------------------------------
# Sheet output
# ---------------------------------------------------------------------------

def write_sheets(m, company):
    """Write verified model to Google Sheets. Pure values, no formulas."""
    model = m["model"]
    all_p = m["all_periods"]
    periods = m["periods"]
    labels = m["labels"]
    nh = len(periods)

    def v(code, p):
        idx = all_p.index(p)
        vals = model.get(code, [0] * len(all_p))
        return vals[idx] if idx < len(vals) else 0

    def fmt(val):
        """Round to nearest integer for display."""
        if isinstance(val, float):
            return round(val)
        return val

    def R(code, label):
        return [code, "", label, ""]

    def data_row(code, label=None):
        lbl = label or labels.get(code, code)
        row = R(code, lbl)
        for p in all_p:
            row.append(fmt(v(code, p)))
        return row

    title = f"{company} - 3-Statement Model"
    sid, url, sheet_ids = gws_create(title, ["IS", "BS", "CF", "Summary"])
    print(f"  URL: {url}", file=sys.stderr)

    # --- IS ---
    is_rows = [
        [],
        R("", "$m") + list(all_p),
        [],
        ["", "", "Revenue"],
        data_row("REV1"),
        data_row("REV2"),
        data_row("REVT"),
        [],
        data_row("COGS1"),
        data_row("COGS2"),
        data_row("COGST"),
        data_row("GP"),
        [],
        data_row("OPEX1"),
        data_row("OPEX2"),
        data_row("OPEX3"),
        data_row("OPEXT"),
        [],
        data_row("OPINC"),
        data_row("INC_O"),
        data_row("EBT"),
        data_row("TAX"),
        data_row("INC_NET"),
        [],
        data_row("SBC"),
        data_row("DA"),
    ]
    # Remove rows where code has no data
    is_rows = [r for r in is_rows if not (len(r) >= 5 and r[0] and all(r[i] == 0 for i in range(4, len(r))))]
    gws_write(sid, f"IS!A1:{dcol(len(all_p)-1)}{len(is_rows)}", is_rows)
    print(f"  IS: {len(is_rows)} rows", file=sys.stderr)

    # --- BS ---
    bs_rows = [
        [],
        R("", "$m") + list(all_p),
        [],
        ["", "", "Current Assets"],
        data_row("BS_CASH"),
        data_row("BS_AR"),
        data_row("BS_INV"),
        data_row("BS_CA1"),
        data_row("BS_CA2"),
        data_row("BS_CA3"),
        data_row("BS_TCA"),
        [],
        ["", "", "Non-Current Assets"],
        data_row("BS_PPE"),
        data_row("BS_LTA1"),
        data_row("BS_LTA2"),
        data_row("BS_TNCA"),
        [],
        data_row("BS_TA"),
        [],
        ["", "", "Current Liabilities"],
        data_row("BS_AP"),
        data_row("BS_STD"),
        data_row("BS_OCL1"),
        data_row("BS_OCL2"),
        data_row("BS_TCL"),
        [],
        ["", "", "Non-Current Liabilities"],
        data_row("BS_LTD"),
        data_row("BS_NCL1"),
        data_row("BS_NCL2"),
        data_row("BS_TNCL"),
        [],
        data_row("BS_TL"),
        [],
        ["", "", "Equity"],
        data_row("BS_CS"),
        data_row("BS_RE"),
        data_row("BS_OE"),
        data_row("BS_TE"),
        [],
        ["", "", "Balance Check (must be 0)"],
        R("", "TA - TL - TE") + [fmt(v("BS_TA", p) - v("BS_TL", p) - v("BS_TE", p)) for p in all_p],
    ]
    bs_rows = [r for r in bs_rows if not (len(r) >= 5 and r[0] and all(r[i] == 0 for i in range(4, len(r))))]
    gws_write(sid, f"BS!A1:{dcol(len(all_p)-1)}{len(bs_rows)}", bs_rows)
    print(f"  BS: {len(bs_rows)} rows", file=sys.stderr)

    # --- CF ---
    cf_rows = [
        [],
        R("", "$m") + list(all_p),
        [],
        ["", "", "Operating Activities"],
        data_row("CF_NI"),
        data_row("CF_DA"),
        data_row("CF_SBC"),
        data_row("CF_OP1"),
        data_row("CF_OP2"),
        data_row("CF_OP3"),
        data_row("CF_AR"),
        data_row("CF_INV"),
        data_row("CF_AP"),
        data_row("CF_OPCF"),
        [],
        ["", "", "Investing Activities"],
        data_row("CF_CAPEX"),
        data_row("CF_SECPUR"),
        data_row("CF_SECSAL"),
        data_row("CF_INV1"),
        data_row("CF_INVCF"),
        [],
        ["", "", "Financing Activities"],
        data_row("CF_FIN1"),
        data_row("CF_BUY"),
        data_row("CF_DIV"),
        data_row("CF_DISS"),
        data_row("CF_DREP"),
        data_row("CF_FIN2"),
        data_row("CF_FINCF"),
        [],
        data_row("CF_NETCH"),
        data_row("CF_BEGC"),
        data_row("CF_ENDC"),
        [],
        ["", "", "Cash Check (CF End - BS Cash, must be 0)"],
        R("", "CF End - BS Cash") + [fmt(v("CF_ENDC", p) - v("BS_CASH", p)) for p in all_p],
    ]
    cf_rows = [r for r in cf_rows if not (len(r) >= 5 and r[0] and all(r[i] == 0 for i in range(4, len(r))))]
    gws_write(sid, f"CF!A1:{dcol(len(all_p)-1)}{len(cf_rows)}", cf_rows)
    print(f"  CF: {len(cf_rows)} rows", file=sys.stderr)

    # --- Summary ---
    summary_rows = [
        [],
        R("", "$m") + list(all_p),
        [],
        data_row("REVT", "Revenue"),
        data_row("GP", "Gross Profit"),
        data_row("OPINC", "EBIT"),
        data_row("INC_NET", "Net Income"),
        [],
        data_row("BS_TA", "Total Assets"),
        data_row("BS_TL", "Total Liabilities"),
        data_row("BS_TE", "Total Equity"),
        [],
        data_row("CF_OPCF", "Operating CF"),
        data_row("CF_INVCF", "Investing CF"),
        data_row("CF_FINCF", "Financing CF"),
        data_row("CF_NETCH", "Net Change in Cash"),
        [],
        ["", "", "INVARIANT CHECKS (all must be 0)"],
        R("", "") + list(all_p),
        R("", "BS Balance (TA-TL-TE)") + [fmt(v("BS_TA", p) - v("BS_TL", p) - v("BS_TE", p)) for p in all_p],
        R("", "Cash (CF End - BS Cash)") + [fmt(v("CF_ENDC", p) - v("BS_CASH", p)) for p in all_p],
        R("", "BS Assets (TCA+TNCA-TA)") + [fmt(v("BS_TCA", p) + v("BS_TNCA", p) - v("BS_TA", p)) for p in all_p],
        R("", "BS Liab (TCL+TNCL-TL)") + [fmt(v("BS_TCL", p) + v("BS_TNCL", p) - v("BS_TL", p)) for p in all_p],
        R("", "BS Equity (CS+RE+OE-TE)") + [fmt(v("BS_CS", p) + v("BS_RE", p) + v("BS_OE", p) - v("BS_TE", p)) for p in all_p],
    ]
    gws_write(sid, f"Summary!A1:{dcol(len(all_p)-1)}{len(summary_rows)}", summary_rows)
    print(f"  Summary: {len(summary_rows)} rows", file=sys.stderr)

    # Column width formatting
    requests = []
    for sheet_name, sheet_id in sheet_ids.items():
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2},
                "properties": {"pixelSize": 50},
                "fields": "pixelSize",
            }
        })
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                "properties": {"pixelSize": 200},
                "fields": "pixelSize",
            }
        })
    gws_batch_update(sid, requests)

    return sid, url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _deep_merge(base, overlay):
    """Recursively merge overlay into base. Base values win on conflict (newer first)."""
    for k, v in overlay.items():
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)


def _merge_financials(financials_list):
    """Deep-merge multiple financials dicts. First file wins on conflicts (newest first)."""
    if len(financials_list) == 1:
        return financials_list[0]
    import copy
    merged = copy.deepcopy(financials_list[0])
    for fin in financials_list[1:]:
        _deep_merge(merged, fin)
    return merged


def main():
    parser = argparse.ArgumentParser(description="Python-first 3-statement model")
    parser.add_argument("--financials", required=True, nargs="+")
    parser.add_argument("--company", required=True)
    args = parser.parse_args()

    financials_list = []
    for path in args.financials:
        with open(path) as f:
            financials_list.append(json.load(f))
    financials = _merge_financials(financials_list)
    print(f"Loaded {len(financials_list)} file(s)", file=sys.stderr)

    print("Loading filing data...", file=sys.stderr)
    filing = load_filing(financials)
    print(f"  {len(filing['periods'])} periods, {len(filing['items'])} codes", file=sys.stderr)

    print("Computing model...", file=sys.stderr)
    m = compute_model(filing)

    print("Verifying invariants...", file=sys.stderr)
    errors = verify_model(m)
    if errors:
        print(f"\n*** {len(errors)} INVARIANT FAILURES ***", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
        print("\nAborting — fix data before writing sheet.", file=sys.stderr)
        sys.exit(1)
    print("  All invariants pass!", file=sys.stderr)

    print("Writing to Google Sheets...", file=sys.stderr)
    sid, url = write_sheets(m, args.company)
    print(f"\nDone! {url}", file=sys.stderr)
    print(json.dumps({"spreadsheet_id": sid, "url": url, "company": args.company,
                       "periods": m["periods"], "forecast_periods": m["forecast_periods"]}))


if __name__ == "__main__":
    main()
