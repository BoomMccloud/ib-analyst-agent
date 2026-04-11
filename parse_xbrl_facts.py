"""
Stage 2b: XBRL Facts Parser
============================
Parses iXBRL tags from SEC filing HTML and maps them to standard model codes.
Pure Python — no LLM, no API keys.

Usage:
  python parse_xbrl_facts.py xbrl_facts.json -o tagged_financials.json
  # Or directly from HTML:
  python parse_xbrl_facts.py --html filing.htm -o tagged_financials.json
"""

import argparse
import json
import re
import sys


# ---------------------------------------------------------------------------
# iXBRL Parsing
# ---------------------------------------------------------------------------

def _parse_xbrl_value(raw: str) -> float | None:
    """Parse an iXBRL numeric value."""
    raw = re.sub(r'<[^>]+>', '', raw).strip()
    raw = raw.replace(',', '').replace(' ', '').replace('\xa0', '')
    # em-dash or en-dash = zero/null
    if not raw or raw in ('—', '–', '-', '\u2014', '\u2013'):
        return None
    neg = False
    if raw.startswith('(') and raw.endswith(')'):
        raw = raw[1:-1]
        neg = True
    try:
        val = float(raw)
        return -val if neg else val
    except ValueError:
        return None


def extract_xbrl_contexts(html: str) -> dict:
    """Parse <xbrli:context> blocks to map id -> period info.

    Returns: {context_id: {"period": "2024-12-31", "is_instant": bool, "has_segment": bool}}
    """
    ctx_pattern = re.compile(
        r'<xbrli:context\s+id="([^"]+)"[^>]*>([\s\S]*?)</xbrli:context>',
        re.IGNORECASE
    )
    instant_pat = re.compile(r'<xbrli:instant>([^<]+)</xbrli:instant>', re.IGNORECASE)
    end_pat = re.compile(r'<xbrli:endDate>([^<]+)</xbrli:endDate>', re.IGNORECASE)
    segment_pat = re.compile(r'xbrldi:', re.IGNORECASE)

    contexts = {}
    for m in ctx_pattern.finditer(html):
        ctx_id = m.group(1)
        body = m.group(2)

        has_segment = bool(segment_pat.search(body))

        instant = instant_pat.search(body)
        if instant:
            contexts[ctx_id] = {
                "period": instant.group(1).strip(),
                "is_instant": True,
                "has_segment": has_segment,
            }
            continue

        end = end_pat.search(body)
        if end:
            contexts[ctx_id] = {
                "period": end.group(1).strip(),
                "is_instant": False,
                "has_segment": has_segment,
            }

    return contexts


def extract_xbrl_facts(html: str) -> list[dict]:
    """Parse all <ix:nonFraction> tags from iXBRL HTML."""
    # Flexible pattern — attributes can appear in any order
    tag_pattern = re.compile(
        r'<ix:nonFraction\s+([^>]*?)>([\s\S]*?)</ix:nonFraction>',
        re.IGNORECASE
    )
    name_pat = re.compile(r'name="([^"]+)"')
    ctx_pat = re.compile(r'contextRef="([^"]+)"')
    sign_pat = re.compile(r'sign="-"')

    facts = []
    for m in tag_pattern.finditer(html):
        attrs = m.group(1)
        raw_value = m.group(2)

        name_m = name_pat.search(attrs)
        ctx_m = ctx_pat.search(attrs)
        if not name_m or not ctx_m:
            continue

        value = _parse_xbrl_value(raw_value)
        if value is None:
            continue

        # Handle sign attribute (negates the value)
        if sign_pat.search(attrs):
            value = -value

        # NOTE: We intentionally do NOT apply the scale attribute.
        # The displayed value in the HTML is already in the filing's stated
        # unit (typically millions). scale="6" just tells processors how to
        # convert back to raw dollars — we want the human-readable value.

        # Extract decimals attribute for precision ranking when duplicates exist.
        dec_pat = re.compile(r'decimals="([^"]+)"')
        dec_m = dec_pat.search(attrs)
        decimals = int(dec_m.group(1)) if dec_m and dec_m.group(1).lstrip("-").isdigit() else 0

        facts.append({
            "tag": name_m.group(1),
            "context": ctx_m.group(1),
            "value": value,
            "decimals": decimals,
        })

    return facts


def build_xbrl_facts_dict(html: str) -> dict:
    """Extract and resolve all XBRL facts from HTML.

    Returns: {tag: {period: value}} — only primary entity, no segments.
    """
    contexts = extract_xbrl_contexts(html)
    raw_facts = extract_xbrl_facts(html)

    facts = {}  # tag -> {period: value}
    precision = {}  # tag -> {period: decimals} — track precision for dedup
    for fact in raw_facts:
        ctx = contexts.get(fact["context"])
        if not ctx:
            continue
        # Skip dimensional/segment contexts (breakdowns, not totals)
        if ctx["has_segment"]:
            continue
        tag = fact["tag"]
        period = ctx["period"]
        dec = fact.get("decimals", 0)
        if tag not in facts:
            facts[tag] = {}
            precision[tag] = {}
        # When duplicate tags exist for the same period (e.g. PFE reports
        # Assets at both scale="6"/decimals="-6" and scale="9"/decimals="-9"),
        # keep the higher-precision value (higher decimals = more precise).
        if period not in facts[tag] or dec > precision[tag].get(period, -999):
            facts[tag][period] = fact["value"]
            precision[tag][period] = dec

    return facts


# ---------------------------------------------------------------------------
# Tag-to-Code Mapping
# ---------------------------------------------------------------------------

IS_TAG_MAP = {
    "REVT": [
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:SalesRevenueNet",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "COGST": [
        "us-gaap:CostOfRevenue",
        "us-gaap:CostOfGoodsAndServicesSold",
        "us-gaap:CostOfGoodsSold",
    ],
    "GP": [
        "us-gaap:GrossProfit",
    ],
    "OPEXT": [
        "us-gaap:OperatingExpenses",
        "us-gaap:CostsAndExpenses",
    ],
    "OPINC": [
        "us-gaap:OperatingIncomeLoss",
    ],
    "INC_O": [
        "us-gaap:NonoperatingIncomeExpense",
        "us-gaap:OtherNonoperatingIncomeExpense",
    ],
    "EBT": [
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ],
    "TAX": [
        "us-gaap:IncomeTaxExpenseBenefit",
    ],
    "INC_NET": [
        "us-gaap:NetIncomeLoss",
        "us-gaap:ProfitLoss",
    ],
    "SBC": [
        "us-gaap:ShareBasedCompensation",
        "us-gaap:AllocatedShareBasedCompensationExpense",
    ],
    "DA": [
        "us-gaap:DepreciationDepletionAndAmortization",
        "us-gaap:DepreciationAndAmortization",
        "us-gaap:Depreciation",
    ],
}

BS_TAG_MAP = {
    "BS_TA":  ["us-gaap:Assets"],
    "BS_TCA": ["us-gaap:AssetsCurrent"],
    "BS_TL":  ["us-gaap:Liabilities"],
    "BS_TCL": ["us-gaap:LiabilitiesCurrent"],
    "BS_TE":  [
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "us-gaap:StockholdersEquity",
    ],
}

CF_TAG_MAP = {
    "CF_OPCF": [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    ],
    "CF_INVCF": [
        "us-gaap:NetCashProvidedByUsedInInvestingActivities",
    ],
    "CF_FINCF": [
        "us-gaap:NetCashProvidedByUsedInFinancingActivities",
    ],
    "CF_NETCH": [
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease",
    ],
    "CF_ENDC": [
        "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    ],
}

# Labels for output
CODE_LABELS = {
    "REVT": "Total Revenue", "COGST": "Cost of Revenue", "GP": "Gross Profit",
    "OPEXT": "Operating Expenses", "OPINC": "Operating Income",
    "INC_O": "Other Income / (Expense)", "EBT": "Earnings Before Tax",
    "TAX": "Income Tax", "INC_NET": "Net Income",
    "SBC": "Stock-Based Compensation", "DA": "Depreciation & Amortization",
    "BS_TA": "Total Assets", "BS_TCA": "Total Current Assets",
    "BS_TNCA": "Total Non-Current Assets",
    "BS_TL": "Total Liabilities", "BS_TCL": "Total Current Liabilities",
    "BS_TNCL": "Total Non-Current Liabilities",
    "BS_TE": "Total Equity",
    "CF_OPCF": "Operating Cash Flow", "CF_INVCF": "Investing Cash Flow",
    "CF_FINCF": "Financing Cash Flow", "CF_NETCH": "Net Change in Cash",
    "CF_BEGC": "Beginning Cash", "CF_ENDC": "Ending Cash",
}


def map_xbrl_to_codes(xbrl_facts: dict) -> dict:
    """Map XBRL tag-keyed facts to model code-keyed facts.

    Returns:
        {"totals": {code: {period: value}},
         "unmapped_tags": int,
         "periods": {period: ["IS"|"BS"|"CF"]}}
    """
    totals = {}
    mapped_tags = set()

    all_maps = [
        ("IS", IS_TAG_MAP),
        ("BS", BS_TAG_MAP),
        ("CF", CF_TAG_MAP),
    ]

    for statement, tag_map in all_maps:
        for code, tag_list in tag_map.items():
            for tag in tag_list:
                if tag in xbrl_facts:
                    totals[code] = xbrl_facts[tag]
                    mapped_tags.add(tag)
                    break

    # Compute derived totals
    if "BS_TA" in totals and "BS_TCA" in totals:
        totals["BS_TNCA"] = {
            p: totals["BS_TA"].get(p, 0) - totals["BS_TCA"].get(p, 0)
            for p in totals["BS_TA"]
            if p in totals["BS_TCA"]
        }
    if "BS_TL" in totals and "BS_TCL" in totals:
        totals["BS_TNCL"] = {
            p: totals["BS_TL"].get(p, 0) - totals["BS_TCL"].get(p, 0)
            for p in totals["BS_TL"]
            if p in totals["BS_TCL"]
        }

    # CF_BEGC from prior-period CF_ENDC
    if "CF_ENDC" in totals:
        periods = sorted(totals["CF_ENDC"].keys())
        begc = {}
        for i, p in enumerate(periods):
            if i > 0:
                begc[p] = totals["CF_ENDC"][periods[i - 1]]
        if begc:
            totals["CF_BEGC"] = begc

    # Determine which periods have which statements
    period_coverage = {}
    for code, vals in totals.items():
        if code.startswith("BS_"):
            stmt = "BS"
        elif code.startswith("CF_"):
            stmt = "CF"
        else:
            stmt = "IS"
        for p in vals:
            if p not in period_coverage:
                period_coverage[p] = set()
            period_coverage[p].add(stmt)
    # Convert sets to lists for JSON
    period_coverage = {p: sorted(stmts) for p, stmts in period_coverage.items()}

    unmapped = len([t for t in xbrl_facts if t not in mapped_tags
                    and t.startswith("us-gaap:")])

    return {
        "totals": totals,
        "periods": period_coverage,
        "unmapped_us_gaap_tags": unmapped,
        "total_tags_parsed": len(xbrl_facts),
    }


def build_structured_from_xbrl(tagged: dict) -> dict:
    """Convert tagged XBRL financials into the structured format expected by pymodel.

    Produces the same structure as structure_financials.py output:
    - income_statement with _flex_categories and _singles
    - balance_sheet with _flex_categories and _totals
    - cash_flows with _flex_categories and _structural
    """
    totals = tagged["totals"]

    # Find complete periods (have IS + BS + CF data)
    periods_info = tagged["periods"]
    complete_periods = sorted([
        p for p, stmts in periods_info.items()
        if "IS" in stmts and "BS" in stmts and "CF" in stmts
    ])

    # --- Income Statement ---
    is_singles = []
    for code in ["REVT", "COGST", "GP", "OPEXT", "OPINC", "INC_O",
                  "EBT", "TAX", "INC_NET", "SBC", "DA"]:
        if code in totals:
            vals = {p: v for p, v in totals[code].items()}
            is_singles.append({
                "code": code,
                "label": CODE_LABELS.get(code, code),
                "values": vals,
            })

    # Build period-first fiscal_years so load_filing can detect periods
    is_periods = set()
    for single in is_singles:
        is_periods.update(single["values"].keys())
    fiscal_years = {}
    for p in sorted(is_periods):
        fiscal_years[p] = {}
        for single in is_singles:
            if p in single["values"]:
                # Use a snake_case key derived from the label
                key = single["label"].lower().replace(" ", "_").replace("/", "_")
                fiscal_years[p][key] = single["values"][p]

    is_data = {
        "unit": "millions",
        "fiscal_years": fiscal_years,
        "_flex_categories": [],
        "_singles": is_singles,
    }

    # --- Balance Sheet ---
    bs_totals_list = []
    for code in ["BS_TA", "BS_TL"]:
        if code in totals:
            bs_totals_list.append({
                "code": code,
                "label": CODE_LABELS.get(code, code),
                "values": totals[code],
            })

    bs_categories = []
    for subtotal_code, label in [
        ("BS_TCA", "Total Current Assets"),
        ("BS_TNCA", "Total Non-Current Assets"),
        ("BS_TCL", "Total Current Liabilities"),
        ("BS_TNCL", "Total Non-Current Liabilities"),
        ("BS_TE", "Total Equity"),
    ]:
        if subtotal_code in totals:
            catch_all_code = {
                "BS_TCA": "BS_CA_OTH", "BS_TNCA": "BS_NCA_OTH",
                "BS_TCL": "BS_CL_OTH", "BS_TNCL": "BS_NCL_OTH",
                "BS_TE": "BS_EQ_OTH",
            }[subtotal_code]
            bs_categories.append({
                "subtotal_code": subtotal_code,
                "subtotal_label": label,
                "subtotal_values": totals[subtotal_code],
                "flex": [],  # No flex items from XBRL alone
                "other": {
                    "code": catch_all_code,
                    "label": "Other",
                    "values": totals[subtotal_code],  # All goes to catch-all
                },
            })

    # Build period-first balance_sheet so load_filing can detect BS periods
    # Only include periods that have Total Assets (the defining BS total)
    bs_ta_periods = set(totals.get("BS_TA", {}).keys())
    balance_sheet_periods = {}
    for p in sorted(bs_ta_periods):
        balance_sheet_periods[p] = {"total_assets": totals["BS_TA"][p]}

    bs_data = {
        "unit": "millions",
        "balance_sheet": balance_sheet_periods,
        "_flex_categories": bs_categories,
        "_totals": bs_totals_list,
    }

    # --- Cash Flows ---
    cf_categories = []
    for subtotal_code, label, catch_all_code in [
        ("CF_OPCF", "Operating Cash Flow", "CF_OP_OTH"),
        ("CF_INVCF", "Investing Cash Flow", "CF_INV_OTH"),
        ("CF_FINCF", "Financing Cash Flow", "CF_FIN_OTH"),
    ]:
        if subtotal_code in totals:
            cf_categories.append({
                "subtotal_code": subtotal_code,
                "subtotal_label": label,
                "subtotal_values": totals[subtotal_code],
                "flex": [],
                "other": {
                    "code": catch_all_code,
                    "label": "Other",
                    "values": totals[subtotal_code],
                },
            })

    cf_structural = []
    for code in ["CF_NETCH", "CF_BEGC", "CF_ENDC"]:
        if code in totals:
            cf_structural.append({
                "code": code,
                "label": CODE_LABELS.get(code, code),
                "values": totals[code],
            })

    cf_data = {
        "unit": "millions",
        "_flex_categories": cf_categories,
        "_structural": cf_structural,
    }

    return {
        "income_statement": is_data,
        "balance_sheet": bs_data,
        "cash_flows": cf_data,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Map XBRL facts to model codes")
    parser.add_argument("input", nargs="?", help="Path to xbrl_facts.json")
    parser.add_argument("--html", help="Path to filing HTML (extracts XBRL inline)")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--structured", action="store_true",
                        help="Output in structure_financials.py format")
    args = parser.parse_args()

    if args.html:
        with open(args.html) as f:
            html = f.read()
        xbrl_facts = build_xbrl_facts_dict(html)
    elif args.input:
        with open(args.input) as f:
            xbrl_facts = json.load(f)
    else:
        print("Error: provide either xbrl_facts.json or --html filing.htm", file=sys.stderr)
        sys.exit(1)

    tagged = map_xbrl_to_codes(xbrl_facts)

    # Report
    print(f"Parsed {tagged['total_tags_parsed']} XBRL tags", file=sys.stderr)
    print(f"Mapped {len(tagged['totals'])} model codes, "
          f"{tagged['unmapped_us_gaap_tags']} unmapped us-gaap tags", file=sys.stderr)

    # Show what we found
    for code in sorted(tagged["totals"]):
        vals = tagged["totals"][code]
        periods_str = ", ".join(f"{p}: {v:,.0f}" for p, v in sorted(vals.items()))
        print(f"  {code:12s} {periods_str}", file=sys.stderr)

    # Check required totals
    required = ["REVT", "INC_NET", "BS_TA", "BS_TL", "BS_TE",
                "CF_OPCF", "CF_INVCF", "CF_FINCF", "CF_ENDC"]
    missing = [c for c in required if c not in tagged["totals"]]
    if missing:
        print(f"\nWARNING: Missing required totals: {missing}", file=sys.stderr)

    # Check invariants
    totals = tagged["totals"]
    complete = sorted([p for p, s in tagged["periods"].items()
                       if "IS" in s and "BS" in s and "CF" in s])
    print(f"\nComplete periods (IS+BS+CF): {complete}", file=sys.stderr)

    errors = []
    for p in complete:
        ta = totals.get("BS_TA", {}).get(p, 0)
        tl = totals.get("BS_TL", {}).get(p, 0)
        te = totals.get("BS_TE", {}).get(p, 0)
        if abs(ta - tl - te) > 0.5:
            errors.append(f"BS Balance {p}: TA={ta:,.0f} != TL+TE={tl+te:,.0f} (diff={ta-tl-te:,.0f})")

    if errors:
        print(f"\nInvariant errors:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
    else:
        print(f"\nAll BS balance invariants pass!", file=sys.stderr)

    # Output
    if args.structured:
        result = build_structured_from_xbrl(tagged)
    else:
        result = tagged

    output = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Saved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
