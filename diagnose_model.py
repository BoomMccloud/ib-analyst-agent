"""
Diagnose model sheet vs actual filing data.
Reads the structured financials JSON and the Google Sheet, compares values.

Usage:
  python diagnose_model.py --financials /tmp/aapl_all_structured.json --sheet-id <spreadsheet_id>
"""

import argparse
import json
import subprocess
import sys


def _parse_num(v):
    """Parse a formatted number string like '1,234' or '-5,678' to float."""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        v = v.strip().replace(",", "")
        if not v or v in ("", "-"):
            return 0
        try:
            return float(v)
        except ValueError:
            return 0
    return 0


def gws_read(sid, range_):
    """Read values from Google Sheet."""
    params = json.dumps({
        "spreadsheetId": sid,
        "range": range_,
        "valueRenderOption": "FORMATTED_VALUE",
    })
    result = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get", "--params", params],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR reading {range_}: {result.stderr}", file=sys.stderr)
        return []
    data = json.loads(result.stdout)
    return data.get("values", [])


def parse_filing_sheet(rows):
    """Parse Filing sheet: code in col A, label in col C, data from col E onward."""
    periods = []
    # Find header row with periods
    for row in rows[:5]:
        for c in row[4:] if len(row) > 4 else []:
            if isinstance(c, str) and len(c) >= 4 and c[:4].isdigit():
                if c not in periods:
                    periods.append(c)

    # Collect all items: {code -> [{label, values: {period: val}}]}
    items = {}
    for row in rows:
        if not row or len(row) < 5:
            continue
        code = row[0] if row[0] else ""
        if not isinstance(code, str) or not code:
            continue
        label = row[2] if len(row) > 2 else ""
        values = {}
        for i, p in enumerate(periods):
            idx = 4 + i
            if idx < len(row) and row[idx] not in ("", None):
                values[p] = _parse_num(row[idx])
        if values:
            if code not in items:
                items[code] = {"label": label, "values": {}}
            # Sum duplicates (SUMIF behavior)
            for p, v in values.items():
                items[code]["values"][p] = items[code]["values"].get(p, 0) + v

    return items, periods


def parse_model_sheet(rows):
    """Parse a model sheet (BS/CF/IS): code in col A, data from col E onward."""
    periods = []
    for row in rows[:5]:
        for c in row[4:] if len(row) > 4 else []:
            if isinstance(c, str) and len(c) >= 4 and c[:4].isdigit():
                if c not in periods:
                    periods.append(c)

    items = {}
    for row in rows:
        if not row or len(row) < 5:
            continue
        code = row[0] if row[0] else ""
        if not isinstance(code, str) or not code:
            continue
        # For model sheets, code might be a formula result from Filing link
        # Skip non-code strings
        if " " in code or len(code) > 15:
            continue
        values = {}
        for i, p in enumerate(periods):
            idx = 4 + i
            if idx < len(row):
                values[p] = _parse_num(row[idx])
        if code not in items:
            items[code] = values
        else:
            # Sum duplicates
            for p, v in values.items():
                items[code][p] = items[code].get(p, 0) + v

    return items, periods


def _deep_find(data, key):
    """Recursively search for a key in nested dicts, return numeric value."""
    if not isinstance(data, dict):
        return None
    if key in data:
        v = data[key]
        if isinstance(v, (int, float)):
            return v
    for v in data.values():
        if isinstance(v, dict):
            result = _deep_find(v, key)
            if result is not None:
                return result
    return None


def extract_filed_values(financials, periods):
    """Extract actual values from the structured financials JSON."""
    filed = {}

    # IS
    is_raw = financials.get("income_statement", {})
    is_data = is_raw.get("fiscal_years", is_raw.get("data", is_raw))
    for code, keys in [
        ("REVT", ["total_net_sales", "revenues", "revenue", "total_revenue"]),
        ("COGST", ["total_cost_of_sales", "cost_of_revenues", "cost_of_revenue"]),
        ("GP", ["gross_margin", "gross_profit"]),
        ("OPINC", ["operating_income", "income_from_operations"]),
        ("INC_NET", ["net_income"]),
    ]:
        filed[code] = {}
        for p in periods:
            for k in keys:
                v = _deep_find(is_data.get(p, {}), k)
                if v is not None:
                    filed[code][p] = v
                    break

    # BS
    bs_raw = financials.get("balance_sheet", {})
    bs_data = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
    for code, keys in [
        ("BS_CASH", ["cash_and_cash_equivalents"]),
        ("BS_TCA", ["total_current_assets"]),
        ("BS_TA", ["total_assets"]),
        ("BS_TCL", ["total_current_liabilities"]),
        ("BS_TL", ["total_liabilities"]),
        ("BS_TE", ["total_shareholders_equity", "total_stockholders_equity"]),
    ]:
        filed[code] = {}
        for p in periods:
            pdata = bs_data.get(p, {})
            for k in keys:
                v = _deep_find(pdata, k)
                if v is not None:
                    filed[code][p] = v
                    break

    # CF
    cf_data = financials.get("cash_flows", {})
    op_cf = cf_data.get("operating_activities", cf_data.get("cash_flows_from_operating_activities", {}))
    inv_cf = cf_data.get("investing_activities", cf_data.get("cash_flows_from_investing_activities", {}))
    fin_cf = cf_data.get("financing_activities", cf_data.get("cash_flows_from_financing_activities", {}))

    for code, section, keys in [
        ("CF_OPCF", op_cf, ["cash_generated_by_operating_activities", "net_cash_provided_by_operating_activities"]),
        ("CF_INVCF", inv_cf, ["cash_generated_by_used_in_investing_activities", "net_cash_used_in_investing_activities"]),
        ("CF_FINCF", fin_cf, ["cash_used_in_financing_activities", "net_cash_used_in_financing_activities"]),
    ]:
        filed[code] = {}
        for k in keys:
            if k in section and isinstance(section[k], dict):
                for p in periods:
                    if p in section[k]:
                        filed[code][p] = section[k][p]
                break

    for container_key, code in [("beginning_balances", "CF_BEGC"), ("ending_balances", "CF_ENDC")]:
        container = cf_data.get(container_key, {})
        filed[code] = {}
        for v in container.values():
            if isinstance(v, dict):
                for p in periods:
                    if p in v:
                        filed[code][p] = v[p]

    for k in ["increase_decrease_in_cash_cash_equivalents_and_restricted_cash_and_cash_equivalents"]:
        if k in cf_data and isinstance(cf_data[k], dict):
            filed["CF_NETCH"] = {p: cf_data[k][p] for p in periods if p in cf_data[k]}

    return filed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--financials", required=True, nargs="+")
    parser.add_argument("--sheet-id", required=True)
    args = parser.parse_args()

    financials = {}
    for path in args.financials:
        with open(path) as f:
            financials.update(json.load(f))

    # Read sheets
    print("Reading sheets...", file=sys.stderr)
    filing_rows = gws_read(args.sheet_id, "Filing!A1:H100")
    filing_data, periods = parse_filing_sheet(filing_rows)
    hist_periods = [p for p in periods if not p.endswith("E")]
    print(f"Periods: {hist_periods}", file=sys.stderr)

    bs_rows = gws_read(args.sheet_id, "BS!A1:L80")
    bs_model, _ = parse_model_sheet(bs_rows)

    cf_rows = gws_read(args.sheet_id, "CF!A1:L45")
    cf_model, _ = parse_model_sheet(cf_rows)

    summary_rows = gws_read(args.sheet_id, "Summary!A1:L55")

    # Filed values from JSON
    filed = extract_filed_values(financials, hist_periods)

    print("\n" + "=" * 80)
    print("DIAGNOSIS: Model Sheet vs Actual Filing Data")
    print("=" * 80)

    # 1. Period coverage
    print("\n--- 1. PERIOD COVERAGE ---")
    bs_periods_with_data = set()
    for code, info in filing_data.items():
        if code.startswith("BS_"):
            for p, v in info["values"].items():
                if v != 0:
                    bs_periods_with_data.add(p)
    missing_bs = set(hist_periods) - bs_periods_with_data
    print(f"  IS/CF periods: {hist_periods}")
    print(f"  BS periods with data: {sorted(bs_periods_with_data)}")
    if missing_bs:
        print(f"  *** BS MISSING: {sorted(missing_bs)} — causes wrong WC calcs for next period!")

    # 2. Misclassified items
    print("\n--- 2. POTENTIAL MISCLASSIFICATIONS (Filing sheet) ---")
    supplemental_kw = ["paid for income", "paid for interest", "received", "supplemental", "non-cash", "right-of-use"]
    found_misclass = False
    for row in filing_rows:
        if not row or len(row) < 3:
            continue
        code = str(row[0]) if row[0] else ""
        label = str(row[2]) if len(row) > 2 else ""
        if not code.startswith("CF_"):
            continue
        for kw in supplemental_kw:
            if kw.lower() in label.lower():
                vals = {hist_periods[i]: row[4+i] for i in range(len(hist_periods)) if 4+i < len(row) and isinstance(row[4+i], (int, float))}
                print(f"  {code:12s} | {label:55s} | {vals}")
                found_misclass = True
                break
    if not found_misclass:
        print("  None found")

    # 3. Filing SUMIF totals vs filed subtotals
    print("\n--- 3. FILING SHEET SUMIF TOTALS vs ACTUAL SUBTOTALS ---")
    print(f"  {'Code':12s} {'Period':12s} {'SUMIF Total':>14s} {'Filed Total':>14s} {'Delta':>12s}")
    print("  " + "-" * 65)

    sumif_vs_filed = [
        ("CF_OPCF", ["CF_NI", "CF_DA", "CF_SBC", "CF_OP1", "CF_OP2", "CF_OP3", "CF_AR", "CF_INV", "CF_AP"]),
        ("CF_INVCF", ["CF_CAPEX", "CF_SECPUR", "CF_SECSAL", "CF_INV1"]),
        ("CF_FINCF", ["CF_FIN1", "CF_FIN2", "CF_BUY", "CF_DIV", "CF_DISS", "CF_DREP"]),
        ("BS_TCA", ["BS_CASH", "BS_AR", "BS_INV", "BS_CA1", "BS_CA2", "BS_CA3"]),
        ("BS_TA", ["BS_TCA", "BS_TNCA"]),
        ("BS_TL", ["BS_TCL", "BS_TNCL"]),
        ("BS_TE", ["BS_CS", "BS_RE", "BS_OE"]),
    ]
    for total_code, component_codes in sumif_vs_filed:
        for p in hist_periods:
            comp_sum = sum(filing_data.get(c, {}).get("values", {}).get(p, 0) for c in component_codes)
            filed_total = filing_data.get(total_code, {}).get("values", {}).get(p, 0)
            delta = comp_sum - filed_total
            flag = " ***" if abs(delta) > 0.5 else ""
            if flag or True:  # show all
                print(f"  {total_code:12s} {p:12s} {comp_sum:>14,.0f} {filed_total:>14,.0f} {delta:>12,.0f}{flag}")

    # 4. Model computed values vs filed values
    print("\n--- 4. MODEL SHEET COMPUTED vs FILED VALUES ---")
    print(f"  {'Code':12s} {'Period':12s} {'Model':>14s} {'Filed':>14s} {'Delta':>12s}")
    print("  " + "-" * 65)

    checks = [
        ("CF_OPCF", cf_model), ("CF_INVCF", cf_model), ("CF_FINCF", cf_model),
        ("CF_NETCH", cf_model), ("CF_BEGC", cf_model), ("CF_ENDC", cf_model),
        ("BS_CASH", bs_model), ("BS_TCA", bs_model), ("BS_TA", bs_model),
        ("BS_TL", bs_model), ("BS_TE", bs_model),
    ]
    for code, model in checks:
        if code not in filed:
            continue
        for p in hist_periods:
            f_val = filed[code].get(p)
            m_val = model.get(code, {}).get(p, 0)
            if f_val is None:
                continue
            delta = m_val - f_val
            flag = " ***" if abs(delta) > 0.5 else ""
            print(f"  {code:12s} {p:12s} {m_val:>14,.0f} {f_val:>14,.0f} {delta:>12,.0f}{flag}")

    # 5. Invariant checks
    print("\n--- 5. INVARIANT CHECKS (from Summary sheet) ---")
    for row in summary_rows:
        if not row or len(row) < 5:
            continue
        label = str(row[2]) if len(row) > 2 else ""
        if any(label.startswith(f"{i}.") for i in range(1, 14)) or "TOTAL ERRORS" in label:
            vals = []
            for i in range(len(hist_periods)):
                idx = 4 + i
                v = row[idx] if idx < len(row) else ""
                vals.append(v if isinstance(v, (int, float)) else 0)
            non_zero = any(v != 0 for v in vals)
            flag = " ***" if non_zero else " OK"
            vals_str = [f"{v:>10,.0f}" if isinstance(v, (int, float)) else f"{'?':>10s}" for v in vals]
            print(f"  {label:45s} {' '.join(vals_str)}{flag}")

    print()


if __name__ == "__main__":
    main()
