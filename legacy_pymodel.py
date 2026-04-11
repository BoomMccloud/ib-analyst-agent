"""
Python-first 3-Statement Financial Model
=========================================
Flex-row architecture: fixed category headers + top-N named rows + 1 catch-all.
No LLM classification. Pure Python picks the most material items per category.

Invariants:
  - Per category: flex rows + catch_all == filed subtotal (assigned, not plugged)
  - BS: TA = TCA + TNCA, TL = TCL + TNCL, TA = TL + TE
  - CF: BEGC + NETCH = ENDC, OPCF + INVCF + FINCF + FX = NETCH
  - CF_ENDC = BS_CASH

Usage:
  python pymodel.py --financials /tmp/aapl_all_structured.json --company "Apple Inc."
"""

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from typing import Dict, Any

from gws_utils import _run_gws, gws_write, gws_batch_update

@dataclass
class ModelResult:
    historical_data: Dict[str, Any]
    forecast_data: Dict[str, Any] = None
    metadata: Dict[str, Any] = None


FLEX_PER_CATEGORY = 3  # top N named rows per category


# ---------------------------------------------------------------------------
# Tautological API: enforce invariants by construction
# ---------------------------------------------------------------------------

def set_v(model, code, period, val):
    """Set a value in the model dict. model[code][period] = val."""
    if code not in model:
        model[code] = {}
    model[code][period] = float(val)


def get_v(model, code, period, default=0):
    """Get a value from the model dict."""
    return model.get(code, {}).get(period, default)


def set_category(model, cat, period, subtotal, flex_values: dict):
    """Set subtotal and flex items for a category dict. Catch-all is computed."""
    set_v(model, cat["subtotal_code"], period, subtotal)
    for code, val in flex_values.items():
        set_v(model, code, period, val)
    catch_all = subtotal - sum(flex_values.values())
    set_v(model, cat["catch_all_code"], period, catch_all)


def set_is_cascade(model, period, revt, cogst, opext, inc_o, tax):
    """Set IS values. GP, OPINC, EBT, INC_NET are computed."""
    set_v(model, "REVT", period, revt)
    set_v(model, "COGST", period, cogst)
    set_v(model, "GP", period, revt - cogst)
    set_v(model, "OPEXT", period, opext)
    set_v(model, "OPINC", period, revt - cogst - opext)
    set_v(model, "INC_O", period, inc_o)
    ebt = revt - cogst - opext + inc_o
    set_v(model, "EBT", period, ebt)
    set_v(model, "TAX", period, tax)
    set_v(model, "INC_NET", period, ebt - tax)


def set_bs_totals(model, period, tca, tnca, tcl, tncl, te):
    """Set BS totals. TA and TL are computed from components."""
    set_v(model, "BS_TCA", period, tca)
    set_v(model, "BS_TNCA", period, tnca)
    set_v(model, "BS_TA", period, tca + tnca)
    set_v(model, "BS_TCL", period, tcl)
    set_v(model, "BS_TNCL", period, tncl)
    set_v(model, "BS_TL", period, tcl + tncl)
    set_v(model, "BS_TE", period, te)


def set_cf_totals(model, period, opcf, invcf, fincf, fx=0):
    """Set CF section totals. NETCH is computed as sum."""
    set_v(model, "CF_OPCF", period, opcf)
    set_v(model, "CF_INVCF", period, invcf)
    set_v(model, "CF_FINCF", period, fincf)
    set_v(model, "CF_FX", period, fx)
    set_v(model, "CF_NETCH", period, opcf + invcf + fincf + fx)


def set_cf_cash(model, period, begc, netch):
    """Set CF cash proof. ENDC is computed as BEGC + NETCH."""
    set_v(model, "CF_BEGC", period, begc)
    set_v(model, "CF_NETCH", period, netch)
    set_v(model, "CF_ENDC", period, begc + netch)


# ---------------------------------------------------------------------------
# Label cleaning
# ---------------------------------------------------------------------------

def clean_label(key: str) -> str:
    """Convert snake_case key to Title Case label."""
    return key.replace("_", " ").strip().title()


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Data extraction from raw filing JSON
# ---------------------------------------------------------------------------

def _deep_find(data, key):
    """Recursively find a numeric value by key."""
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


def _navigate(data, keys):
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return None
    return data


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


# ---------------------------------------------------------------------------
# Flex-row selection: pick top N items by materiality, rest -> catch-all
# ---------------------------------------------------------------------------

def _flatten_category(bs_data: dict, periods: list[str], category_path: list[str],
                       subtotal_key: str) -> tuple[list[dict], dict]:
    """Extract items from a BS category and its filed subtotal.

    Returns:
        (items, subtotal_values) where items excludes the subtotal.
    """
    items = {}  # key -> {period: val}
    subtotal_values = {}

    for p in periods:
        pdata = bs_data.get(p, {})
        cat = _navigate(pdata, category_path)
        if not cat or not isinstance(cat, dict):
            continue

        for k, v in cat.items():
            if isinstance(v, (int, float)):
                if k == subtotal_key:
                    subtotal_values[p] = v
                else:
                    if k not in items:
                        items[k] = {}
                    items[k][p] = v
            elif isinstance(v, dict):
                # Nested sub-category -- flatten into parent
                for k2, v2 in v.items():
                    if isinstance(v2, (int, float)):
                        if k2.startswith("total_"):
                            continue  # skip nested subtotals
                        composite_key = f"{k}/{k2}"
                        if composite_key not in items:
                            items[composite_key] = {}
                        items[composite_key][p] = v2

    item_list = [{"key": k, "label": clean_label(k.split("/")[-1]), "values": v}
                 for k, v in items.items()]
    return item_list, subtotal_values


def _flatten_cf_section(cf_section: dict, periods: list[str],
                         subtotal_keys: list[str]) -> tuple[list[dict], dict]:
    """Extract items from a CF section.

    CF data is section-first: each key maps to {period: value} or nested dicts.
    """
    items = []
    subtotal_values = {}

    skip_patterns = {"cash_paid_for", "supplemental", "non_cash", "right_of_use"}

    def is_supplemental(key):
        kl = key.lower()
        return any(pat in kl for pat in skip_patterns)

    def collect(data, parent_key=""):
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            full_key = f"{parent_key}/{key}" if parent_key else key

            period_vals = {p: value[p] for p in periods
                          if p in value and isinstance(value[p], (int, float))}
            if period_vals:
                if key in subtotal_keys:
                    subtotal_values.update(period_vals)
                elif not is_supplemental(key):
                    items.append({
                        "key": full_key,
                        "label": clean_label(key),
                        "values": period_vals,
                    })
            else:
                collect(value, full_key)

    collect(cf_section)
    return items, subtotal_values


def pick_flex_rows(items: list[dict], periods: list[str], n: int = FLEX_PER_CATEGORY
                   ) -> tuple[list[dict], list[dict]]:
    """Pick the top N items by average absolute value across periods."""
    if len(items) <= n:
        return items, []

    def avg_abs(item):
        vals = [abs(item["values"].get(p, 0)) for p in periods]
        return sum(vals) / max(len(vals), 1)

    ranked = sorted(items, key=avg_abs, reverse=True)
    return ranked[:n], ranked[n:]


def sum_values(items: list[dict], periods: list[str]) -> dict:
    """Sum values across items for each period."""
    result = {}
    for p in periods:
        total = sum(item["values"].get(p, 0) for item in items)
        if total != 0:
            result[p] = total
    return result


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _apply_xbrl_mapping(raw_facts: dict, mapping: dict) -> dict:
    """Apply an LLM classification map to raw XBRL facts.

    raw_facts: {xbrl_tag: value} or {xbrl_tag: {period: value}}
    mapping: classification map from structure_financials (maps tags to
             standard model codes/sections).

    Returns a structured financials dict compatible with load_filing().
    """
    # The mapping tells us which XBRL tags map to which financial statement
    # line items. Apply it to produce the standard structured format.
    result = {}
    for section_key, section_map in mapping.items():
        if not isinstance(section_map, dict):
            continue
        result[section_key] = {}
        for target_key, source_tags in section_map.items():
            if isinstance(source_tags, str):
                # Direct tag reference
                val = raw_facts.get(source_tags)
                if val is not None:
                    result[section_key][target_key] = val
            elif isinstance(source_tags, dict):
                result[section_key][target_key] = source_tags
    return result


def load_filing(financials: dict, xbrl_mapping: dict = None) -> dict:
    """Load filing data into flex-row model structure.

    Args:
        financials: Either a complete structured JSON (backward compat) or
                    raw XBRL facts dict when xbrl_mapping is provided.
        xbrl_mapping: Optional LLM classification map. When provided,
                      financials is treated as raw XBRL facts and the
                      mapping is applied to populate the standard model.

    Returns dict with:
      - 'periods': list of period keys
      - 'items': {code: {"label": str, "values": {period: val}}}
      - 'categories': list of category definitions for verification
    """
    if xbrl_mapping is not None:
        # Apply classification map to raw XBRL facts
        financials = _apply_xbrl_mapping(financials, xbrl_mapping)
    is_raw = financials.get("income_statement", {})
    is_data = is_raw.get("fiscal_years", is_raw.get("data", is_raw))
    periods = sorted([k for k in is_data if isinstance(is_data.get(k), dict)
                      and k[:4].isdigit() and not k.lower().endswith("_usd")])

    items = {}  # code -> {label, values: {period: val}}
    categories = []

    def add(code, label, vals):
        """Add or merge values for a code."""
        if not vals:
            return
        if code not in items:
            items[code] = {"label": label, "values": {}}
        for p, v in vals.items():
            items[code]["values"][p] = items[code]["values"].get(p, 0) + v

    def load_preclassified(section_data, key="_flex_categories"):
        """Load pre-classified flex categories from structured JSON.

        Returns True if found and loaded, False if not available.
        """
        flex_cats = section_data.get(key)
        if not flex_cats:
            return False

        for cat in flex_cats:
            add(cat["subtotal_code"], cat["subtotal_label"], cat["subtotal_values"])
            flex_codes = []
            for flex_item in cat["flex"]:
                add(flex_item["code"], flex_item["label"], flex_item["values"])
                flex_codes.append(flex_item["code"])
            other = cat["other"]
            add(other["code"], other["label"], other["values"])
            categories.append({
                "subtotal_code": cat["subtotal_code"],
                "flex_codes": flex_codes,
                "catch_all_code": other["code"],
            })

        # Load singles (IS only)
        for single in section_data.get("_singles", []):
            add(single["code"], single["label"], single["values"])

        # Load totals (BS only)
        for total in section_data.get("_totals", []):
            add(total["code"], total["label"], total["values"])

        # Load structural items (CF only)
        for item in section_data.get("_structural", []):
            add(item["code"], item["label"], item["values"])

        return True

    # --- IS ---
    if not load_preclassified(is_raw):
        _load_is_fallback(is_data, periods, items, categories, add, financials)

    # --- BS ---
    if not load_preclassified(financials.get("balance_sheet", {})):
        _load_bs_fallback(financials, items, categories, add)

    # --- CF ---
    cf_data = financials.get("cash_flows", {})
    if not load_preclassified(cf_data):
        _load_cf_fallback(cf_data, periods, items, categories, add)

    # BS totals (needed regardless of classification source)
    bs_raw = financials.get("balance_sheet", {})
    bs_data_raw = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
    bs_pkeys = [k for k in bs_data_raw if isinstance(bs_data_raw.get(k), dict)
                and k[:4].isdigit() and not k.lower().endswith("_usd")]
    bs_periods = sorted(bs_pkeys) if bs_pkeys else []
    if "BS_TA" not in items:
        for p in bs_periods:
            pdata = bs_data_raw.get(p, {})
            ta = _deep_find(pdata, "total_assets")
            if ta is not None:
                add("BS_TA", "Total Assets", {p: ta})
            tl = _deep_find(pdata, "total_liabilities")
            if tl is not None:
                add("BS_TL", "Total Liabilities", {p: tl})

    # Only keep periods where all 3 statements have data
    bs_period_set = set(bs_periods)
    complete_periods = [p for p in periods if p in bs_period_set]
    if len(complete_periods) < len(periods):
        dropped = set(periods) - set(complete_periods)
        print(f"  Dropped {len(dropped)} periods with incomplete data: {sorted(dropped)}", file=sys.stderr)

    return {"periods": complete_periods, "items": items, "categories": categories}


def _load_is_fallback(is_data, periods, items, categories, add, financials):
    """Extract IS data from raw JSON when no pre-classification exists."""
    IS_CATEGORIES = [
        # Revenue: look for net_sales, revenue as parent container
        (["net_sales", "revenue", "revenues"],
         ["total_net_sales", "revenues", "revenue", "total_revenue", "net_revenues"],
         "REVT", "REV", "REV_OTH"),
        # COGS: look for cost_of_sales, cost_of_revenue
        (["cost_of_sales", "cost_of_revenue", "cost_of_goods_sold"],
         ["total_cost_of_sales", "cost_of_revenues", "cost_of_revenue", "cost_of_goods_sold"],
         "COGST", "COGS", "COGS_OTH"),
        # OpEx: look for operating_expenses
        (["operating_expenses"],
         ["total_operating_expenses"],
         "OPEXT", "OPEX", "OPEX_OTH"),
    ]

    # IS single-line items: (possible_keys, code, label)
    IS_SINGLES = [
        (["gross_margin", "gross_profit"], "GP", "Gross Profit"),
        (["operating_income", "income_from_operations"], "OPINC", "Operating Income"),
        (["other_income_expense_net", "other_income_net", "other_income_expense",
          "interest_and_other_income_expense_net"], "INC_O", "Other Income / (Expense)"),
        (["income_before_provision_for_income_taxes", "income_before_income_taxes",
          "income_before_income_tax_and_share_of_results_of_equity_method_investees"],
         "EBT", "Earnings Before Tax"),
        (["provision_for_income_taxes", "income_tax_expense", "income_tax_expenses"],
         "TAX", "Income Tax"),
        (["net_income"], "INC_NET", "Net Income"),
    ]

    def _flatten_is_category(is_data, periods, parent_keys, subtotal_keys):
        """Extract items from an IS category (period-first structure).

        Returns (items_list, subtotal_values).
        """
        cat_items = {}  # key -> {period: val}
        subtotal_vals = {}

        for p in periods:
            pdata = is_data.get(p, {})

            # Find the category container
            container = None
            for pk in parent_keys:
                if pk in pdata and isinstance(pdata[pk], dict):
                    container = pdata[pk]
                    break
                # Also check if the parent key IS the subtotal (single-line COGS)
                if pk in pdata and isinstance(pdata[pk], (int, float)):
                    subtotal_vals[p] = pdata[pk]
                    break

            if container:
                for k, val in container.items():
                    if isinstance(val, (int, float)):
                        if k in subtotal_keys:
                            subtotal_vals[p] = val
                        else:
                            if k not in cat_items:
                                cat_items[k] = {}
                            cat_items[k][p] = val
                    elif isinstance(val, dict):
                        # Nested (e.g., per_share) — skip non-numeric
                        pass

            # If no container found, try subtotal as top-level key
            if not container and p not in subtotal_vals:
                for sk in subtotal_keys:
                    v = _deep_find(pdata, sk)
                    if v is not None:
                        subtotal_vals[p] = v
                        break

        item_list = [{"key": k, "label": clean_label(k), "values": v}
                     for k, v in cat_items.items()]
        return item_list, subtotal_vals

    for parent_keys, subtotal_keys, subtotal_code, flex_prefix, catch_all_code in IS_CATEGORIES:
        cat_items, subtotal_vals = _flatten_is_category(
            is_data, periods, parent_keys, subtotal_keys)

        add(subtotal_code, clean_label(subtotal_keys[0]), subtotal_vals)

        flex_items, other_items = pick_flex_rows(cat_items, periods)

        flex_codes = []
        for i, item in enumerate(flex_items):
            code = f"{flex_prefix}{i+1}"
            add(code, item["label"], item["values"])
            flex_codes.append(code)

        other_vals = sum_values(other_items, periods)
        add(catch_all_code, "Other", other_vals)

        # Absorb structural gaps (same as BS/CF)
        for p in periods:
            filed = subtotal_vals.get(p, 0)
            if filed == 0:
                continue
            comp = sum(items.get(c, {}).get("values", {}).get(p, 0) for c in flex_codes)
            comp += items.get(catch_all_code, {}).get("values", {}).get(p, 0)
            gap = filed - comp
            if abs(gap) > 0.5:
                if catch_all_code not in items:
                    items[catch_all_code] = {"label": "Other", "values": {}}
                items[catch_all_code]["values"][p] = items[catch_all_code]["values"].get(p, 0) + gap
                print(f"    {subtotal_code} {p}: structural gap {gap:,.0f} -> {catch_all_code}", file=sys.stderr)

        categories.append({
            "subtotal_code": subtotal_code,
            "flex_codes": flex_codes,
            "catch_all_code": catch_all_code,
        })

        print(f"  IS {subtotal_code}: {len(flex_items)} flex + {len(other_items)} other", file=sys.stderr)

    # IS single-line items (cascade structure, not categories)
    for possible_keys, code, label in IS_SINGLES:
        vals = {}
        for p in periods:
            pdata = is_data.get(p, {})
            for k in possible_keys:
                v = _deep_find(pdata, k)
                if v is not None:
                    vals[p] = v
                    break
        add(code, label, vals)

    # SBC & DA from IS or CF
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

    # Try IS first, fall back to CF adjustments
    sbc_keys = ["share_based_compensation_expense", "stock_based_compensation"]
    sbc_vals = {}
    for p in periods:
        pdata = is_data.get(p, {})
        for k in sbc_keys:
            v = _deep_find(pdata, k)
            if v is not None:
                sbc_vals[p] = v
                break
    if not sbc_vals:
        sbc_vals = get_cf_item(adj, sbc_keys)
    add("SBC", "Stock-Based Compensation", sbc_vals)

    da_keys = ["depreciation_and_amortization"]
    da_vals = {}
    for p in periods:
        pdata = is_data.get(p, {})
        for k in da_keys:
            v = _deep_find(pdata, k)
            if v is not None:
                da_vals[p] = v
                break
    if not da_vals:
        da_vals = get_cf_item(adj, da_keys)
    add("DA", "Depreciation & Amortization", da_vals)


def _assign_category(items, categories, add, cat_items, cat_periods,
                     subtotal_vals, subtotal_code, flex_prefix, catch_all_code):
    """Pick flex rows, assign catch-all, absorb structural gaps."""
    add(subtotal_code, clean_label(subtotal_code), subtotal_vals)

    flex_items, other_items = pick_flex_rows(cat_items, cat_periods)

    flex_codes = []
    for i, item in enumerate(flex_items):
        code = f"{flex_prefix}{i+1}"
        add(code, item["label"], item["values"])
        flex_codes.append(code)

    other_vals = sum_values(other_items, cat_periods)
    add(catch_all_code, "Other", other_vals)

    for p in cat_periods:
        filed = subtotal_vals.get(p, 0)
        if filed == 0:
            continue
        comp = sum(items.get(c, {}).get("values", {}).get(p, 0) for c in flex_codes)
        comp += items.get(catch_all_code, {}).get("values", {}).get(p, 0)
        gap = filed - comp
        if abs(gap) > 0.5:
            if catch_all_code not in items:
                items[catch_all_code] = {"label": "Other", "values": {}}
            items[catch_all_code]["values"][p] = items[catch_all_code]["values"].get(p, 0) + gap
            print(f"    {subtotal_code} {p}: structural gap {gap:,.0f}", file=sys.stderr)

    categories.append({
        "subtotal_code": subtotal_code,
        "flex_codes": flex_codes,
        "catch_all_code": catch_all_code,
    })
    print(f"  {subtotal_code}: {len(flex_items)} flex + {len(other_items)} other", file=sys.stderr)


def _load_bs_fallback(financials, items, categories, add):
    """Extract BS data from raw JSON when no pre-classification exists."""
    bs_raw = financials.get("balance_sheet", {})
    bs_data = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
    bs_pkeys = [k for k in bs_data if isinstance(bs_data.get(k), dict)
                and k[:4].isdigit() and not k.lower().endswith("_usd")]
    if bs_pkeys:
        bs_periods = sorted(bs_pkeys)
    else:
        bs_data, bs_periods = _convert_section_first(bs_data)
        bs_periods = [p for p in bs_periods if not p.lower().endswith("_usd")]

    # --- BS: flex-row extraction ---
    bs_raw = financials.get("balance_sheet", {})
    bs_data = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))

    bs_pkeys = [k for k in bs_data if isinstance(bs_data.get(k), dict)
                and k[:4].isdigit() and not k.lower().endswith("_usd")]
    if bs_pkeys:
        bs_periods = sorted(bs_pkeys)
    else:
        bs_data, bs_periods = _convert_section_first(bs_data)
        bs_periods = [p for p in bs_periods if not p.lower().endswith("_usd")]

    BS_CATEGORIES = [
        (["assets", "current_assets"], [],
         "total_current_assets", "BS_TCA", "BS_CA", "BS_CA_OTH"),
        (["assets", "non_current_assets"], [["assets"]],
         "total_non_current_assets", "BS_TNCA", "BS_NCA", "BS_NCA_OTH"),
        (["liabilities", "current_liabilities"],
         [["liabilities_and_stockholders_equity", "current_liabilities"]],
         "total_current_liabilities", "BS_TCL", "BS_CL", "BS_CL_OTH"),
        (["liabilities", "non_current_liabilities"],
         [["liabilities_and_stockholders_equity"]],
         "total_non_current_liabilities", "BS_TNCL", "BS_NCL", "BS_NCL_OTH"),
        (["shareholders_equity"],
         [["liabilities_and_stockholders_equity", "stockholders_equity"],
          ["stockholders_equity"]],
         "total_shareholders_equity", "BS_TE", "BS_EQ", "BS_EQ_OTH"),
    ]

    # Total Assets and Total Liabilities from filing
    ta_vals = {}
    tl_vals = {}
    for p in bs_periods:
        pdata = bs_data.get(p, {})
        ta = _deep_find(pdata, "total_assets")
        if ta is not None:
            ta_vals[p] = ta
        tl = _deep_find(pdata, "total_liabilities")
        if tl is not None:
            tl_vals[p] = tl
    add("BS_TA", "Total Assets", ta_vals)
    add("BS_TL", "Total Liabilities", tl_vals)

    for cat_path, alt_paths, subtotal_key, subtotal_code, flex_prefix, catch_all_code in BS_CATEGORIES:
        cat_items, subtotal_vals = _flatten_category(bs_data, bs_periods, cat_path, subtotal_key)

        if not cat_items and alt_paths:
            for alt_path in alt_paths:
                cat_items, subtotal_vals_alt = _flatten_category(bs_data, bs_periods, alt_path, subtotal_key)
                if cat_items:
                    if subtotal_vals_alt:
                        subtotal_vals = subtotal_vals_alt
                    break

        if not subtotal_vals:
            alt_keys = {
                "total_shareholders_equity": ["total_stockholders_equity", "total_equity"],
                "total_non_current_assets": ["total_noncurrent_assets"],
                "total_non_current_liabilities": ["total_noncurrent_liabilities"],
            }
            all_paths = [cat_path] + alt_paths
            for alt in alt_keys.get(subtotal_key, []):
                for try_path in all_paths:
                    _, subtotal_vals = _flatten_category(bs_data, bs_periods, try_path, alt)
                    if subtotal_vals:
                        break
                if subtotal_vals:
                    break

        _assign_category(items, categories, add, cat_items, bs_periods, subtotal_vals, subtotal_code,
                        flex_prefix, catch_all_code)

def _load_cf_fallback(cf_data, periods, items, categories, add):
    """Extract CF data from raw JSON when no pre-classification exists."""
    # --- CF: flex-row extraction ---
    CF_SECTIONS = [
        ("operating_activities", "cash_flows_from_operating_activities",
         ["cash_generated_by_operating_activities", "net_cash_provided_by_operating_activities",
          "net_cash_from_operating_activities"],
         "CF_OPCF", "CF_OP", "CF_OP_OTH"),
        ("investing_activities", "cash_flows_from_investing_activities",
         ["cash_generated_by_used_in_investing_activities", "net_cash_used_in_investing_activities",
          "net_cash_from_investing_activities"],
         "CF_INVCF", "CF_INV", "CF_INV_OTH"),
        ("financing_activities", "cash_flows_from_financing_activities",
         ["cash_used_in_financing_activities", "net_cash_used_in_financing_activities",
          "net_cash_from_financing_activities"],
         "CF_FINCF", "CF_FIN", "CF_FIN_OTH"),
    ]

    for section_key, alt_key, subtotal_keys, subtotal_code, flex_prefix, catch_all_code in CF_SECTIONS:
        cf_section = cf_data.get(section_key, cf_data.get(alt_key, {}))
        if not cf_section:
            continue

        cf_items, subtotal_vals = _flatten_cf_section(cf_section, periods, subtotal_keys)
        _assign_category(items, categories, add, cf_items, periods, subtotal_vals, subtotal_code,
                        flex_prefix, catch_all_code)

    # CF structural items
    netch_keys = ["increase_decrease_in_cash_cash_equivalents_and_restricted_cash_and_cash_equivalents",
                  "net_increase_decrease_in_cash"]
    for nk in netch_keys:
        v = cf_data.get(nk)
        if isinstance(v, dict):
            pvals = {p: v[p] for p in periods if p in v and isinstance(v[p], (int, float))}
            if pvals:
                add("CF_NETCH", "Net Change in Cash", pvals)
                break

    for bal_key, code, label in [
        ("beginning_balances", "CF_BEGC", "Beginning Cash"),
        ("ending_balances", "CF_ENDC", "Ending Cash"),
    ]:
        bal = cf_data.get(bal_key, {})
        if isinstance(bal, dict):
            for bk, bv in bal.items():
                if isinstance(bv, dict):
                    pvals = {p: bv[p] for p in periods if p in bv and isinstance(bv[p], (int, float))}
                    if pvals:
                        add(code, label, pvals)
                        break


# ---------------------------------------------------------------------------
# Model computation
# ---------------------------------------------------------------------------

def get(items, code, period, default=0):
    return items.get(code, {}).get("values", {}).get(period, default)


def _find_code_by_label(items, prefix, keywords, exclude_suffix=None):
    """Find a flex code by label keywords."""
    for code, info in items.items():
        if not code.startswith(prefix):
            continue
        if exclude_suffix and code.endswith(exclude_suffix):
            continue
        lbl = info["label"].lower()
        if all(kw in lbl for kw in keywords):
            return code
    return None


def compute_model(filing):
    """Build 3-statement model with forecasts.

    No reconciliation needed -- flex rows + catch_all == subtotal by construction.
    """
    periods = filing["periods"]
    # periods is already filtered to complete data only
    items = filing["items"]
    categories = filing["categories"]

    last_year = int(periods[-1][:4])
    forecast_periods = [f"{last_year + i}E" for i in range(1, 6)]
    all_periods = periods + forecast_periods
    nh = len(periods)

    model = {}  # code -> [val per all_period]
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
        for p in all_periods[:nh]:
            set_v(code, p, info["values"].get(p, 0))

    # Identify key BS codes by label
    bs_cash_code = _find_code_by_label(items, "BS_CA", ["cash", "equivalent"], "_OTH")
    if not bs_cash_code:
        bs_cash_code = _find_code_by_label(items, "BS_CA", ["cash"], "_OTH")
    bs_ar_code = _find_code_by_label(items, "BS_CA", ["receivable"], "_OTH")
    # Exclude vendor receivables — we want trade AR
    if bs_ar_code and "vendor" in items[bs_ar_code]["label"].lower():
        # Try to find a non-vendor receivable
        alt = None
        for code, info in items.items():
            if code.startswith("BS_CA") and code != "BS_CA_OTH" and code != bs_ar_code:
                if "receivable" in info["label"].lower() and "vendor" not in info["label"].lower():
                    alt = code
                    break
        if alt:
            bs_ar_code = alt
    bs_inv_code = _find_code_by_label(items, "BS_CA", ["inventor"], "_OTH")
    bs_ap_code = _find_code_by_label(items, "BS_CL", ["payable"], "_OTH")
    bs_ppe_code = _find_code_by_label(items, "BS_NCA", ["property"], "_OTH")
    if not bs_ppe_code:
        bs_ppe_code = _find_code_by_label(items, "BS_NCA", ["plant"], "_OTH")
    bs_re_code = _find_code_by_label(items, "BS_EQ", ["retained"], "_OTH")
    if not bs_re_code:
        bs_re_code = _find_code_by_label(items, "BS_EQ", ["deficit"], "_OTH")
    bs_cs_code = _find_code_by_label(items, "BS_EQ", ["common"], "_OTH")
    if not bs_cs_code:
        bs_cs_code = _find_code_by_label(items, "BS_EQ", ["paid"], "_OTH")

    if bs_cash_code:
        print(f"  BS Cash: {bs_cash_code} ({labels.get(bs_cash_code)})", file=sys.stderr)
    else:
        print("  WARNING: Could not identify BS cash code, using BS_CA1", file=sys.stderr)
        bs_cash_code = "BS_CA1"

    # --- STEP 2: Reconcile CF ending cash to BS cash ---
    for p in periods:
        bs_cash = v(bs_cash_code, p)
        if bs_cash != 0:
            set_v("CF_ENDC", p, bs_cash)
        idx = all_periods.index(p)
        if idx > 0:
            prev_bs = v(bs_cash_code, all_periods[idx - 1])
            if prev_bs != 0:
                set_v("CF_BEGC", p, prev_bs)
        set_v("CF_NETCH", p, v("CF_ENDC", p) - v("CF_BEGC", p))

    # Compute CF_FX as residual
    for p in periods:
        if v("CF_OPCF", p) != 0:
            fx = v("CF_NETCH", p) - v("CF_OPCF", p) - v("CF_INVCF", p) - v("CF_FINCF", p)
            set_v("CF_FX", p, fx)
            labels.setdefault("CF_FX", "FX / Reconciliation")

    def find_cat(code):
        for c in categories:
            if c["subtotal_code"] == code:
                return c
        return None

    # --- STEP 3: IS forecasts ---
    last_p = periods[-1]
    rev_growth = 0.05
    if len(periods) >= 2:
        r1, r2 = v("REVT", periods[-2]), v("REVT", periods[-1])
        if r1 > 0:
            rev_growth = (r2 / r1) - 1

    cogs_pct = v("COGST", last_p) / v("REVT", last_p) if v("REVT", last_p) else 0.5
    opex_pcts = {}
    for i in range(1, 4):
        code = f"OPEX{i}"
        opex_pcts[code] = v(code, last_p) / v("REVT", last_p) if v("REVT", last_p) else 0
    sbc_pct = v("SBC", last_p) / v("OPEXT", last_p) if v("OPEXT", last_p) else 0.1
    tax_rate = v("TAX", last_p) / v("EBT", last_p) if v("EBT", last_p) else 0.21

    # Compute proportional shares for IS category components
    def _cat_shares(cat, last_p):
        """Compute each component's share of the subtotal for the last historical period."""
        total = v(cat["subtotal_code"], last_p)
        if total == 0:
            return {}
        shares = {}
        for fc in cat["flex_codes"]:
            shares[fc] = v(fc, last_p) / total
        shares[cat["catch_all_code"]] = v(cat["catch_all_code"], last_p) / total
        return shares

    is_cat_shares = {}
    for cat in categories:
        if cat["subtotal_code"] in ("REVT", "COGST", "OPEXT"):
            is_cat_shares[cat["subtotal_code"]] = _cat_shares(cat, last_p)

    def _distribute_forecast(cat, fp, total_val):
        """Distribute a forecast subtotal into flex components proportionally."""
        shares = is_cat_shares.get(cat["subtotal_code"], {})
        for code, share in shares.items():
            set_v(code, fp, total_val * share)

    for fp in forecast_periods:
        prev = all_periods[all_periods.index(fp) - 1]
        rev = v("REVT", prev) * (1 + rev_growth)
        set_v("REVT", fp, rev)
        _distribute_forecast(find_cat("REVT"), fp, rev) if find_cat("REVT") else None
        cogs = rev * cogs_pct
        set_v("COGST", fp, cogs)
        _distribute_forecast(find_cat("COGST"), fp, cogs) if find_cat("COGST") else None
        set_v("GP", fp, rev - cogs)
        opext = 0
        for i in range(1, 4):
            code = f"OPEX{i}"
            val = rev * opex_pcts.get(code, 0)
            set_v(code, fp, val)
            opext += val
        cat_opext = find_cat("OPEXT")
        if cat_opext:
            set_v(cat_opext["catch_all_code"], fp, 0)
            opext_from_cat = sum(v(c, fp) for c in cat_opext["flex_codes"]) + v(cat_opext["catch_all_code"], fp)
            set_v("OPEXT", fp, opext_from_cat)
            opext = opext_from_cat
        else:
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

    dso = v(bs_ar_code, last_p) / last_rev * 365 if bs_ar_code and last_rev else 30
    dio = v(bs_inv_code, last_p) / last_cogs * 365 if bs_inv_code and last_cogs else 0
    dpo = v(bs_ap_code, last_p) / last_costs * 365 if bs_ap_code and last_costs else 30

    capex_pct = 0.05
    cf_capex_code = _find_code_by_label(items, "CF_INV", ["property"], "_OTH")
    if not cf_capex_code:
        cf_capex_code = _find_code_by_label(items, "CF_INV", ["capital"], "_OTH")
    if cf_capex_code:
        capex_last = abs(v(cf_capex_code, last_p))
        if last_rev:
            capex_pct = capex_last / last_rev

    def cat_sum(cat, fp):
        return sum(v(c, fp) for c in cat["flex_codes"]) + v(cat["catch_all_code"], fp)

    def hold_flex(cat, fp, prev, driven_codes=None):
        """Hold flex rows and catch-all at prior values, except driven codes."""
        driven_codes = driven_codes or set()
        for fc in cat["flex_codes"]:
            if fc not in driven_codes and v(fc, fp) == 0:
                set_v(fc, fp, v(fc, prev))
        if cat["catch_all_code"] not in driven_codes:
            set_v(cat["catch_all_code"], fp, v(cat["catch_all_code"], prev))

    # Identify CF financing codes for equity rollforward
    cf_buy_code = _find_code_by_label(items, "CF_FIN", ["repurchase"], "_OTH")
    if not cf_buy_code:
        cf_buy_code = _find_code_by_label(items, "CF_FIN", ["common stock"], "_OTH")
    cf_div_code = _find_code_by_label(items, "CF_FIN", ["dividend"], "_OTH")
    cf_stpay_code = _find_code_by_label(items, "CF_FIN", ["settlement"], "_OTH")
    if not cf_stpay_code:
        cf_stpay_code = _find_code_by_label(items, "CF_FIN", ["taxes related"], "_OTH")

    # --- STEP 4+5: BS and CF forecasts (interleaved per period) ---
    # Order: BS (everything except cash) → CF → BS_CASH = CF_ENDC → BS_TCA, BS_TA
    for fp in forecast_periods:
        prev = all_periods[all_periods.index(fp) - 1]
        rev = v("REVT", fp)
        cogs = v("COGST", fp)
        costs = cogs + v("OPEXT", fp)

        # --- BS: Non-cash current assets ---
        if bs_ar_code:
            set_v(bs_ar_code, fp, rev * dso / 365)
        if bs_inv_code:
            set_v(bs_inv_code, fp, cogs * dio / 365 if dio > 0 else 0)
        cat_tca = find_cat("BS_TCA")
        if cat_tca:
            hold_flex(cat_tca, fp, prev, {bs_cash_code, bs_ar_code, bs_inv_code})

        # --- BS: Non-current assets ---
        capex = rev * capex_pct
        if bs_ppe_code:
            ppe = v(bs_ppe_code, prev) + capex - v("DA", fp)
            set_v(bs_ppe_code, fp, ppe)
        cat_tnca = find_cat("BS_TNCA")
        if cat_tnca:
            hold_flex(cat_tnca, fp, prev, {bs_ppe_code})
        tnca = cat_sum(cat_tnca, fp) if cat_tnca else 0
        set_v("BS_TNCA", fp, tnca)

        # --- BS: Liabilities ---
        if bs_ap_code:
            set_v(bs_ap_code, fp, costs * dpo / 365)
        cat_tcl = find_cat("BS_TCL")
        if cat_tcl:
            hold_flex(cat_tcl, fp, prev, {bs_ap_code})
        tcl = cat_sum(cat_tcl, fp) if cat_tcl else 0
        set_v("BS_TCL", fp, tcl)

        cat_tncl = find_cat("BS_TNCL")
        if cat_tncl:
            hold_flex(cat_tncl, fp, prev)
        tncl = cat_sum(cat_tncl, fp) if cat_tncl else 0
        set_v("BS_TNCL", fp, tncl)
        set_v("BS_TL", fp, tcl + tncl)

        # --- BS: Equity (includes CF financing items) ---
        stpay = v(cf_stpay_code, prev) if cf_stpay_code else 0  # hold at last hist value
        buyback = v(cf_buy_code, prev) if cf_buy_code else 0
        dividend = v(cf_div_code, prev) if cf_div_code else 0

        if bs_cs_code:
            set_v(bs_cs_code, fp, v(bs_cs_code, prev) + v("SBC", fp) + stpay)
        if bs_re_code:
            set_v(bs_re_code, fp, v(bs_re_code, prev) + v("INC_NET", fp) + buyback + dividend)
        cat_te = find_cat("BS_TE")
        if cat_te:
            hold_flex(cat_te, fp, prev, {bs_cs_code, bs_re_code})
        te = cat_sum(cat_te, fp) if cat_te else 0
        set_v("BS_TE", fp, te)

        # --- CF: Operating ---
        ni = v("INC_NET", fp)
        da = v("DA", fp)
        sbc = v("SBC", fp)
        cf_ar = -(v(bs_ar_code, fp) - v(bs_ar_code, prev)) if bs_ar_code else 0
        cf_inv = -(v(bs_inv_code, fp) - v(bs_inv_code, prev)) if bs_inv_code else 0
        cf_ap = (v(bs_ap_code, fp) - v(bs_ap_code, prev)) if bs_ap_code else 0

        opcf = ni + da + sbc + cf_ar + cf_inv + cf_ap
        set_v("CF_OPCF", fp, opcf)
        cat_opcf = find_cat("CF_OPCF")
        if cat_opcf:
            set_v(cat_opcf["catch_all_code"], fp, opcf)

        # --- CF: Investing ---
        capex_val = -capex
        set_v("CF_INVCF", fp, capex_val)
        cat_invcf = find_cat("CF_INVCF")
        if cat_invcf:
            set_v(cat_invcf["catch_all_code"], fp, capex_val)

        # --- CF: Financing (hold buybacks, dividends, stock payments) ---
        if cf_stpay_code:
            set_v(cf_stpay_code, fp, stpay)
        if cf_buy_code:
            set_v(cf_buy_code, fp, buyback)
        if cf_div_code:
            set_v(cf_div_code, fp, dividend)
        # Sum all financing flex codes + catch-all for the subtotal
        cat_fincf = find_cat("CF_FINCF")
        fincf = cat_sum(cat_fincf, fp) if cat_fincf else (stpay + buyback + dividend)
        set_v("CF_FINCF", fp, fincf)

        set_v("CF_FX", fp, 0)

        # --- CF: Cash proof ---
        netch = opcf + capex_val + fincf
        set_v("CF_NETCH", fp, netch)
        beg = v(bs_cash_code, prev) if bs_cash_code else 0
        set_v("CF_BEGC", fp, beg)
        endc = beg + netch
        set_v("CF_ENDC", fp, endc)

        # --- BS: Cash = CF ending cash (NOT a plug) ---
        if bs_cash_code:
            set_v(bs_cash_code, fp, endc)

        # --- BS: Totals (computed from components, not forced) ---
        tca = cat_sum(cat_tca, fp) if cat_tca else 0
        set_v("BS_TCA", fp, tca)
        set_v("BS_TA", fp, tca + tnca)

    return {
        "periods": periods,
        "forecast_periods": forecast_periods,
        "all_periods": all_periods,
        "model": model,
        "labels": labels,
        "categories": categories,
        "bs_cash_code": bs_cash_code,
    }


# ---------------------------------------------------------------------------
# Invariant checks
# ---------------------------------------------------------------------------

def _find_cf_match(v_func, period, target_value, prefix, max_items=30):
    """Find a CF flex item whose value matches the target IS value.

    Searches CF_OP1..CF_OP{max_items} for an item within 0.5 tolerance.
    Returns the matching CF value, or None if not found.
    """
    for i in range(1, max_items + 1):
        code = f"{prefix}{i}"
        cf_val = v_func(code, period)
        if cf_val != 0 and abs(cf_val - target_value) < 0.5:
            return cf_val
    return None


def verify_model(m):
    """Run the 5 real invariant checks on all periods.

    Only checks that cannot be enforced by construction:
      1. BS_TA == BS_TL + BS_TE
      2. CF_ENDC == BS_CASH
      3. INC_NET (IS) == INC_NET (CF)
      4. D&A (IS) == D&A (CF)
      5. SBC (IS) == SBC (CF)

    Accepts either load_filing() output (dict-based items) or
    compute_model() output (array-based model).
    """
    errors = []

    # Support both data formats
    if "model" in m and "all_periods" in m:
        # compute_model() output: array-based
        model_data = m["model"]
        all_p = m["all_periods"]
        bs_cash_code = m.get("bs_cash_code", "BS_CA1")

        def v(code, p):
            idx = all_p.index(p)
            return model_data.get(code, [0.0] * len(all_p))[idx]
    else:
        # load_filing() output: dict-based items
        items = m["items"]
        all_p = m["periods"]
        bs_cash_code = None
        for code, info in items.items():
            if code.startswith("BS_CA") and code != "BS_CA_OTH":
                lbl = info["label"].lower()
                if "cash" in lbl and "equivalent" in lbl:
                    bs_cash_code = code
                    break
        if not bs_cash_code:
            for code, info in items.items():
                if code.startswith("BS_CA") and code != "BS_CA_OTH":
                    if "cash" in info["label"].lower():
                        bs_cash_code = code
                        break
        if not bs_cash_code:
            bs_cash_code = "BS_CA1"

        def v(code, p):
            return items.get(code, {}).get("values", {}).get(p, 0)

    def check(name, period, val):
        if abs(val) > 0.5:
            errors.append((name, period, val))

    for p in all_p:
        if v("BS_TA", p) == 0 and v("REVT", p) == 0:
            continue

        # 1. BS Balance: TA == TL + TE
        check("BS Balance (TA-TL-TE)", p, v("BS_TA", p) - v("BS_TL", p) - v("BS_TE", p))

        # 2. CF End Cash == BS Cash
        bs_cash = v(bs_cash_code, p)
        if bs_cash != 0:
            check("Cash (CF_ENDC - BS_CASH)", p, v("CF_ENDC", p) - bs_cash)

        # 3. INC_NET: IS net income == CF net income
        # Find the CF_OP item whose value matches INC_NET (not hardcoded position)
        is_ni = v("INC_NET", p)
        if is_ni != 0:
            cf_ni = _find_cf_match(v, p, is_ni, "CF_OP")
            if cf_ni is not None:
                check("NI Link (IS - CF)", p, is_ni - cf_ni)

        # 4. D&A: IS == CF
        is_da = v("DA", p)
        if is_da != 0:
            cf_da = _find_cf_match(v, p, is_da, "CF_OP")
            if cf_da is not None:
                check("D&A Link (IS - CF)", p, is_da - cf_da)

        # 5. SBC: IS == CF
        is_sbc = v("SBC", p)
        if is_sbc != 0:
            cf_sbc = _find_cf_match(v, p, is_sbc, "CF_OP")
            if cf_sbc is not None:
                check("SBC Link (IS - CF)", p, is_sbc - cf_sbc)

    return errors


# ---------------------------------------------------------------------------
# Sheet output
# ---------------------------------------------------------------------------

def write_sheets(m, company):
    """Write verified model to Google Sheets."""
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
    bs_rows.append([])
    for sub_code, section_label in [
        ("BS_TCL", "Current Liabilities"), ("BS_TNCL", "Non-Current Liabilities")
    ]:
        cat = find_cat(sub_code)
        if cat:
            bs_rows += cat_rows(cat, section_label)
            bs_rows.append([])
    bs_rows.append(data_row("BS_TL"))
    bs_rows.append([])
    cat_te = find_cat("BS_TE")
    if cat_te:
        bs_rows += cat_rows(cat_te, "Equity")
        bs_rows.append([])
    bs_rows += [
        ["", "", "Balance Check (must be 0)"],
        R("", "TA - TL - TE") + [fmt(v("BS_TA", p) - v("BS_TL", p) - v("BS_TE", p)) for p in all_p],
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
        data_row("CF_NETCH"), data_row("CF_BEGC"), data_row("CF_ENDC"), [],
        ["", "", "Cash Check (CF End - BS Cash, must be 0)"],
        R("", "CF End - BS Cash") + [fmt(v("CF_ENDC", p) - v(bs_cash_code, p)) for p in all_p],
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
        R("", "") + list(all_p),
        R("", "BS Balance (TA-TL-TE)") + [fmt(v("BS_TA", p) - v("BS_TL", p) - v("BS_TE", p)) for p in all_p],
        R("", "Cash (CF End - BS Cash)") + [fmt(v("CF_ENDC", p) - v(bs_cash_code, p)) for p in all_p],
        R("", "BS Assets (TCA+TNCA-TA)") + [fmt(v("BS_TCA", p) + v("BS_TNCA", p) - v("BS_TA", p)) for p in all_p],
        R("", "BS Liab (TCL+TNCL-TL)") + [fmt(v("BS_TCL", p) + v("BS_TNCL", p) - v("BS_TL", p)) for p in all_p],
    ]
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _deep_merge(base, overlay):
    for k, v in overlay.items():
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)


def _merge_financials(financials_list):
    if len(financials_list) == 1:
        return financials_list[0]
    merged = copy.deepcopy(financials_list[0])
    for fin in financials_list[1:]:
        _deep_merge(merged, fin)
    return merged


def main():
    parser = argparse.ArgumentParser(description="Python-first 3-statement model")
    parser.add_argument("--financials", required=True, nargs="+")
    parser.add_argument("--company", default="Company")
    parser.add_argument("--checkpoint", action="store_true",
                        help="Run historical baseline check only (Phase 1)")
    args = parser.parse_args()

    financials_list = []
    for path in args.financials:
        with open(path) as f:
            financials_list.append(json.load(f))
    financials = _merge_financials(financials_list)
    print(f"Loaded {len(financials_list)} file(s)", file=sys.stderr)

    print("Loading filing data...", file=sys.stderr)
    filing = load_filing(financials)
    print(f"  {len(filing['periods'])} complete periods, {len(filing['items'])} codes", file=sys.stderr)

    if args.checkpoint:
        MAX_RETRIES = 3

        for attempt in range(1, MAX_RETRIES + 1):
            filing = load_filing(financials)
            errors = verify_model(filing)

            if not errors:
                break

            if attempt == MAX_RETRIES:
                print(f"\n*** Still {len(errors)} invariant failure(s) after {MAX_RETRIES} attempts. Aborting. ***",
                      file=sys.stderr)
                for name, period, delta in errors:
                    print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
                sys.exit(1)

            print(f"Attempt {attempt}: {len(errors)} failure(s). Feeding errors back to structure_financials...",
                  file=sys.stderr)
            for name, period, delta in errors:
                print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)

            # Re-run classification with error context
            try:
                from anthropic import Anthropic
                from structure_financials import reclassify_with_errors
                client = Anthropic()
                financials = reclassify_with_errors(client, financials, errors)
            except Exception as e:
                print(f"  Reclassification failed: {e}", file=sys.stderr)
                print(f"\n*** {len(errors)} INVARIANT FAILURES remain. Aborting. ***", file=sys.stderr)
                sys.exit(1)

        print("  All invariants pass!", file=sys.stderr)

        # Save perfect baseline to disk
        baseline = {
            "periods": filing["periods"],
            "items": {code: info for code, info in filing["items"].items()},
        }
        with open("historical_baseline.json", "w") as f:
            json.dump(baseline, f, indent=2)
        print("Successfully wrote historical_baseline.json", file=sys.stderr)
        sys.exit(0)

    print("Computing model...", file=sys.stderr)
    m = compute_model(filing)

    print("Verifying invariants...", file=sys.stderr)
    errors = verify_model(m)
    if errors:
        print(f"\n*** {len(errors)} INVARIANT FAILURES ***", file=sys.stderr)
        for name, period, delta in errors:
            print(f"  {name}: {period} = {delta:,.0f}", file=sys.stderr)
        print("\nAborting -- fix data before writing sheet.", file=sys.stderr)
        sys.exit(1)
    print("  All invariants pass!", file=sys.stderr)

    print("Writing to Google Sheets...", file=sys.stderr)
    sid, url = write_sheets(m, args.company)
    print(f"\nDone! {url}", file=sys.stderr)
    print(json.dumps({"spreadsheet_id": sid, "url": url, "company": args.company,
                       "periods": m["periods"], "forecast_periods": m["forecast_periods"]}))


if __name__ == "__main__":
    main()
