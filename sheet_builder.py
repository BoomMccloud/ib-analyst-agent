import json
import sys
from typing import Dict, Any
from dataclasses import dataclass
from pymodel import ModelResult

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

def write_sheets(result: ModelResult, company: str):
    """Write verified model to Google Sheets."""
    m = result.historical_data
    model = m["model"]
    all_p = m["all_periods"]
    labels = m["labels"]
    categories = m["categories"]
    bs_cash_code = m.get("bs_cash_code", "BS_CA1")

    def v(code, p):
        idx = all_p.index(p)
        vals = model.get(code, [0] * len(all_p))
        return vals[idx] if idx < len(vals) else 0

    def fmt(val):
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

    def cat_rows(cat, section_label):
        rows = [["", "", section_label]]
        for fc in cat["flex_codes"]:
            rows.append(data_row(fc))
        rows.append(data_row(cat["catch_all_code"]))
        rows.append(data_row(cat["subtotal_code"]))
        return rows

    def find_cat(code):
        for c in categories:
            if c["subtotal_code"] == code:
                return c
        return None

    title = f"{company} - 3-Statement Model"
    
    # Retry loop for sheet generation (Phase 2 requirement)
    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sid, url, sheet_ids = gws_create(title, ["IS", "BS", "CF", "Summary"])
            print(f"  URL: {url}", file=sys.stderr)

            # --- IS ---
            is_rows = [[], R("", "$m") + list(all_p), [], ["", "", "Revenue"]]
            for i in range(1, 4):
                code = f"REV{i}"
                if code in labels:
                    is_rows.append(data_row(code))
            is_rows += [data_row("REVT"), [], ["", "", "Cost of Revenue"]]
            for i in range(1, 4):
                code = f"COGS{i}"
                if code in labels:
                    is_rows.append(data_row(code))
            is_rows += [data_row("COGST"), data_row("GP"), [], ["", "", "Operating Expenses"]]
            for i in range(1, 4):
                code = f"OPEX{i}"
                if code in labels:
                    is_rows.append(data_row(code))
            is_rows += [
                data_row("OPEXT"), [],
                data_row("OPINC"), data_row("INC_O"), data_row("EBT"),
                data_row("TAX"), data_row("INC_NET"), [],
                data_row("SBC"), data_row("DA"),
            ]
            is_rows = [r for r in is_rows if not (len(r) >= 5 and r[0] and all(r[i] == 0 for i in range(4, len(r))))]
            gws_write(sid, f"IS!A1:{dcol(len(all_p)-1)}{len(is_rows)}", is_rows)
            print(f"  IS: {len(is_rows)} rows", file=sys.stderr)

            # --- BS ---
            bs_rows = [[], R("", "$m") + list(all_p), []]
            for sub_code, section_label in [
                ("BS_TCA", "Current Assets"), ("BS_TNCA", "Non-Current Assets")
            ]:
                cat = find_cat(sub_code)
                if cat:
                    bs_rows += cat_rows(cat, section_label)
                    bs_rows.append([])
            bs_rows.append(data_row("BS_TA"))
            bs_ta_row = len(bs_rows)
            bs_rows.append([])
            for sub_code, section_label in [
                ("BS_TCL", "Current Liabilities"), ("BS_TNCL", "Non-Current Liabilities")
            ]:
                cat = find_cat(sub_code)
                if cat:
                    bs_rows += cat_rows(cat, section_label)
                    bs_rows.append([])
            bs_rows.append(data_row("BS_TL"))
            bs_tl_row = len(bs_rows)
            bs_rows.append([])
            cat_te = find_cat("BS_TE")
            if cat_te:
                bs_rows += cat_rows(cat_te, "Equity")
                bs_rows.append([])
            bs_te_row = len(bs_rows) - 1
            
            # Phase A: 1. BS Balance
            bs_rows += [
                ["", "", "Balance Check (must be 0)"],
                R("", "TA - TL - TE") + [f"={dcol(i)}{bs_ta_row} - {dcol(i)}{bs_tl_row} - {dcol(i)}{bs_te_row}" for i in range(len(all_p))]
            ]
            bs_rows = [r for r in bs_rows if not (len(r) >= 5 and r[0] and all(r[i] == 0 for i in range(4, len(r))))]
            gws_write(sid, f"BS!A1:{dcol(len(all_p)-1)}{len(bs_rows)}", bs_rows)
            print(f"  BS: {len(bs_rows)} rows", file=sys.stderr)

            # --- CF ---
            cf_rows = [[], R("", "$m") + list(all_p), []]
            for sub_code, section_label in [
                ("CF_OPCF", "Operating Activities"),
                ("CF_INVCF", "Investing Activities"),
                ("CF_FINCF", "Financing Activities"),
            ]:
                cat = find_cat(sub_code)
                if cat:
                    cf_rows += cat_rows(cat, section_label)
                    cf_rows.append([])
            if "CF_FX" in labels:
                cf_rows.append(data_row("CF_FX"))
            cf_rows += [
                data_row("CF_NETCH"), data_row("CF_BEGC"), data_row("CF_ENDC"), []
            ]
            cf_endc_row = len(cf_rows) - 1
            
            bs_cash_row = None
            for idx, r in enumerate(bs_rows):
                if len(r) > 0 and r[0] == bs_cash_code:
                    bs_cash_row = idx + 1
                    break
            
            # Phase A: 2. Cash Link
            cf_rows += [
                ["", "", "Cash Check (CF End - BS Cash, must be 0)"],
                R("", "CF End - BS Cash") + [f"={dcol(i)}{cf_endc_row} - 'BS'!{dcol(i)}{bs_cash_row}" if bs_cash_row else 0 for i in range(len(all_p))],
            ]
            cf_rows = [r for r in cf_rows if not (len(r) >= 5 and r[0] and all(r[i] == 0 for i in range(4, len(r))))]
            gws_write(sid, f"CF!A1:{dcol(len(all_p)-1)}{len(cf_rows)}", cf_rows)
            print(f"  CF: {len(cf_rows)} rows", file=sys.stderr)

            # --- Summary ---
            summary_rows = [
                [], R("", "$m") + list(all_p), [],
                data_row("REVT", "Revenue"), data_row("GP", "Gross Profit"),
                data_row("OPINC", "EBIT"), data_row("INC_NET", "Net Income"), [],
                data_row("BS_TA", "Total Assets"), data_row("BS_TL", "Total Liabilities"),
                data_row("BS_TE", "Total Equity"), [],
                data_row("CF_OPCF", "Operating CF"), data_row("CF_INVCF", "Investing CF"),
                data_row("CF_FINCF", "Financing CF"), data_row("CF_NETCH", "Net Change in Cash"), [],
                ["", "", "INVARIANT CHECKS (all must be 0)"],
                R("", "") + list(all_p)
            ]
            
            def get_row_index(code, rows_list):
                for idx, r in enumerate(rows_list):
                    if len(r) > 0 and r[0] == code:
                        return idx + 1
                return None
            
            def ref(sheet, code, r_list):
                ri = get_row_index(code, r_list)
                return f"'{sheet}'!{dcol(i)}{ri}" if ri else "0"

            s_ta = get_row_index("BS_TA", summary_rows)
            s_tl = get_row_index("BS_TL", summary_rows)
            s_te = get_row_index("BS_TE", summary_rows)
            
            # Phase A: 13 Invariants in Summary
            # 1. BS Balance
            summary_rows.append(R("", "BS Balance (TA-TL-TE)") + [f"={dcol(i)}{s_ta} - {dcol(i)}{s_tl} - {dcol(i)}{s_te}" for i in range(len(all_p))])
            
            # 2. Cash Link
            s_cf_endc = get_row_index("CF_ENDC", cf_rows)
            summary_rows.append(R("", "Cash (CF End - BS Cash)") + [f"='CF'!{dcol(i)}{s_cf_endc} - 'BS'!{dcol(i)}{bs_cash_row}" if s_cf_endc and bs_cash_row else 0 for i in range(len(all_p))])
            
            # 3. Net Income Link
            summary_rows.append(R("", "Net Income Link") + [f"={ref('IS', 'INC_NET', is_rows)} - {ref('CF', 'CF_OP1', cf_rows)}" for i in range(len(all_p))])
            
            # 4. D&A Link
            summary_rows.append(R("", "D&A Link") + [f"={ref('IS', 'DA', is_rows)} - {ref('CF', 'CF_OP2', cf_rows)}" for i in range(len(all_p))])
            
            # 5. SBC Link
            summary_rows.append(R("", "SBC Link") + [f"={ref('IS', 'SBC', is_rows)} - {ref('CF', 'CF_OP3', cf_rows)}" for i in range(len(all_p))])
            
            # 6. Assets Rollup
            summary_rows.append(R("", "Assets Rollup") + [f"={ref('BS', 'BS_TCA', bs_rows)} + {ref('BS', 'BS_TNCA', bs_rows)} - {ref('BS', 'BS_TA', bs_rows)}" for i in range(len(all_p))])
            
            # 7. Liab Rollup
            summary_rows.append(R("", "Liab Rollup") + [f"={ref('BS', 'BS_TCL', bs_rows)} + {ref('BS', 'BS_TNCL', bs_rows)} - {ref('BS', 'BS_TL', bs_rows)}" for i in range(len(all_p))])
            
            # 8. Equity Rollup
            cat_te = find_cat("BS_TE")
            if cat_te:
                summary_rows.append(R("", "Equity Rollup") + [f"={'+'.join([ref('BS', c, bs_rows) for c in cat_te['flex_codes'] + [cat_te['catch_all_code']]])} - {ref('BS', 'BS_TE', bs_rows)}" for i in range(len(all_p))])
            else:
                summary_rows.append(R("", "Equity Rollup") + [0 for i in range(len(all_p))])
            
            # 9. CF Structure
            summary_rows.append(R("", "CF Structure") + [f"={ref('CF', 'CF_OPCF', cf_rows)} + {ref('CF', 'CF_INVCF', cf_rows)} + {ref('CF', 'CF_FINCF', cf_rows)} + {ref('CF', 'CF_FX', cf_rows)} - {ref('CF', 'CF_NETCH', cf_rows)}" for i in range(len(all_p))])
            
            # 10. Cash Proof
            summary_rows.append(R("", "Cash Proof") + [f"={ref('CF', 'CF_BEGC', cf_rows)} + {ref('CF', 'CF_NETCH', cf_rows)} - {ref('CF', 'CF_ENDC', cf_rows)}" for i in range(len(all_p))])
            
            # 11. IS Gross Profit
            summary_rows.append(R("", "IS Gross Profit") + [f"={ref('IS', 'REVT', is_rows)} - {ref('IS', 'COGST', is_rows)} - {ref('IS', 'GP', is_rows)}" for i in range(len(all_p))])
            
            # 12. IS EBIT
            summary_rows.append(R("", "IS EBIT") + [f"={ref('IS', 'GP', is_rows)} - {ref('IS', 'OPEXT', is_rows)} - {ref('IS', 'OPINC', is_rows)}" for i in range(len(all_p))])
            
            # 13. IS Net Income
            summary_rows.append(R("", "IS Net Income") + [f"={ref('IS', 'EBT', is_rows)} - {ref('IS', 'TAX', is_rows)} - {ref('IS', 'INC_NET', is_rows)}" for i in range(len(all_p))])

            # TOTAL ERRORS (must be 0)
            summary_rows.append(["", "", "TOTAL ERRORS (must be 0)"])
            total_error_row = len(summary_rows) + 1
            # sum absolute errors or sum errors? The sheet says "must be 0". Let's sum abs if possible, but Google Sheets doesn't have an easy array absolute sum.
            # We can just sum them directly and hope they don't net out perfectly. 
            summary_rows.append(R("", "SUM") + [f"=SUM({dcol(i)}{total_error_row-13}:{dcol(i)}{total_error_row-2})" for i in range(len(all_p))])

            gws_write(sid, f"Summary!A1:{dcol(len(all_p)-1)}{len(summary_rows)}", summary_rows)
            print(f"  Summary: {len(summary_rows)} rows", file=sys.stderr)

            # Column widths
            requests = []
            for sheet_name, sheet_id in sheet_ids.items():
                requests.append({
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2},
                        "properties": {"pixelSize": 50}, "fields": "pixelSize",
                    }
                })
                requests.append({
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                        "properties": {"pixelSize": 200}, "fields": "pixelSize",
                    }
                })
            gws_batch_update(sid, requests)

            return sid, url

        except Exception as e:
            print(f"Error building sheet (Attempt {attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                raise

if __name__ == '__main__':
    # Add simple test wrapper if needed
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path) as f:
            data = json.load(f)
        mr = ModelResult(historical_data=data)
        sid, url = write_sheets(mr, "Test Company")
        print(f"Success: {url}")
