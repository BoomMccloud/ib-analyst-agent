"""
Financial Data Structurer
=========================
Takes extracted section text files from extract_sections.py and uses LLMs
to structure them into clean JSON.

- Financial statements (income, balance, cash flow, equity, comprehensive income)
  → Sonnet (small text, needs precision)
- Notes & MD&A → Haiku (large text, cheaper)

Usage:
  python structure_financials.py /tmp/aapl_sections --output results.json
  python structure_financials.py /tmp/baba_sections --output results.json

Requires: ANTHROPIC_API_KEY env var
"""

import argparse
import json
import os
import sys

from anthropic import Anthropic

SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"

FINANCIAL_STATEMENT_PROMPT = """\
Extract all numerical data from this financial statement into structured JSON.

Rules:
- Use the EXACT numbers from the text. Do not calculate or infer.
- All monetary values should be numbers (not strings), in the unit stated (millions, thousands, etc.).
- Include the unit in a top-level "unit" field (e.g., "millions", "thousands").
- Use snake_case for field names.
- Include ALL line items shown, even subtotals.
- For per-share data, include in a separate "per_share" section.

CRITICAL — Output structure MUST be period-first (fiscal year end date as top-level key):

For Income Statements, use "fiscal_years" as the container:
{{
  "unit": "millions",
  "fiscal_years": {{
    "2024-09-28": {{
      "revenue": 391035,
      "cost_of_revenue": 210352,
      "operating_expenses": {{
        "research_and_development": 31370,
        "selling_general_and_administrative": 26097,
        "total_operating_expenses": 57467
      }},
      "operating_income": 123216,
      ...
    }},
    "2023-09-30": {{ ... }}
  }}
}}

For Balance Sheets, use "balance_sheet" as the container, with each period containing
the full asset/liability/equity hierarchy:
{{
  "unit": "millions",
  "balance_sheet": {{
    "2024-09-28": {{
      "assets": {{
        "current_assets": {{
          "cash_and_cash_equivalents": 29943,
          "accounts_receivable_net": 33410,
          ...
          "total_current_assets": 152987
        }},
        "non_current_assets": {{
          "property_plant_and_equipment_net": 45680,
          ...
          "total_non_current_assets": 211993
        }},
        "total_assets": 364980
      }},
      "liabilities": {{
        "current_liabilities": {{ ... }},
        "non_current_liabilities": {{ ... }},
        "total_liabilities": 308030
      }},
      "shareholders_equity": {{
        "common_stock_and_additional_paid_in_capital": 83276,
        "retained_earnings": -19154,
        "accumulated_other_comprehensive_loss": -7172,
        "total_shareholders_equity": 56950
      }},
      "total_liabilities_and_shareholders_equity": 364980
    }},
    "2023-09-30": {{ ... }}
  }}
}}

For Cash Flow Statements, use section-based keys within each period:
{{
  "unit": "millions",
  "operating_activities": {{
    "net_income": {{"2024-09-28": 93736, "2023-09-30": 96995}},
    "adjustments_to_reconcile_net_income": {{
      "depreciation_and_amortization": {{"2024-09-28": 11445, "2023-09-30": 11519}},
      "share_based_compensation_expense": {{"2024-09-28": 12747, "2023-09-30": 10833}}
    }},
    "changes_in_operating_assets_and_liabilities": {{
      "accounts_receivable_net": {{"2024-09-28": -5144, "2023-09-30": -1688}}
    }},
    "cash_generated_by_operating_activities": {{"2024-09-28": 118254, "2023-09-30": 110543}}
  }},
  "investing_activities": {{ ... }},
  "financing_activities": {{ ... }},
  "beginning_balances": {{
    "cash_cash_equivalents_and_restricted_cash_and_cash_equivalents": {{"2024-09-28": 30737}}
  }},
  "ending_balances": {{
    "cash_cash_equivalents_and_restricted_cash_and_cash_equivalents": {{"2024-09-28": 29943}}
  }}
}}

Return ONLY valid JSON, no markdown, no explanation.

{section_type} text:
---
{text}
---
"""

MDA_PROMPT = """\
You are a financial analyst. Extract the key information from this Management's Discussion & Analysis section.

Structure your response as JSON with these sections:
{{
  "business_overview": "Brief description of what the company does",
  "revenue_segments": [
    {{
      "name": "segment name",
      "description": "what it includes",
      "key_drivers": ["driver1", "driver2"],
      "trends": "growth/decline and why"
    }}
  ],
  "expense_discussion": [
    {{
      "category": "e.g., R&D, S&M, G&A, COGS",
      "trends": "what's driving changes",
      "as_pct_of_revenue": "if mentioned"
    }}
  ],
  "key_metrics": ["any KPIs mentioned: DAU, MAU, GMV, ARPU, subscribers, etc."],
  "guidance_or_outlook": "any forward-looking statements",
  "risks_highlighted": ["key risks mentioned in MD&A"],
  "capital_allocation": "capex, buybacks, dividends, M&A discussed"
}}

Return ONLY valid JSON. Be thorough but concise.

MD&A text:
---
{text}
---
"""

NOTES_PROMPT = """\
You are a financial analyst. Extract ALL material information from these Notes to Financial Statements.

First, identify what notes/topics are actually present in the text. Then extract the key data from each one.

For each note you find, include the numerical data and key policies. Common notes include (but are not limited to):
- Revenue recognition and revenue breakdown (by segment, geography, product)
- Segment reporting with per-segment financials
- Property, plant & equipment
- Debt and borrowings
- Share-based compensation
- Income taxes
- Acquisitions and divestitures
- Leases
- Fair value measurements
- Commitments and contingencies
- Related party transactions
- VIE structures (common in Chinese companies)
- Goodwill and intangibles
- Investments

Use the actual note topics from the filing as your JSON keys (in snake_case).
Include numerical data where available. Omit notes not present in the text.

Return ONLY valid JSON.

Notes text:
---
{text}
---
"""

SECTION_CONFIGS = {
    "income_statement": {
        "model": SONNET,
        "prompt": FINANCIAL_STATEMENT_PROMPT,
        "section_type": "Income Statement / Statement of Operations",
    },
    "comprehensive_income": {
        "model": SONNET,
        "prompt": FINANCIAL_STATEMENT_PROMPT,
        "section_type": "Statement of Comprehensive Income",
    },
    "balance_sheet": {
        "model": SONNET,
        "prompt": FINANCIAL_STATEMENT_PROMPT,
        "section_type": "Balance Sheet",
    },
    "shareholders_equity": {
        "model": SONNET,
        "prompt": FINANCIAL_STATEMENT_PROMPT,
        "section_type": "Statement of Changes in Shareholders' Equity",
    },
    "cash_flows": {
        "model": SONNET,
        "prompt": FINANCIAL_STATEMENT_PROMPT,
        "section_type": "Statement of Cash Flows",
    },
    "notes": {
        "model": HAIKU,
        "prompt": NOTES_PROMPT,
        "max_chars": 200_000,
        "max_tokens": 16384,
    },
    "mda": {
        "model": HAIKU,
        "prompt": MDA_PROMPT,
        "max_chars": 200_000,
    },
}


def call_llm(client: Anthropic, model: str, prompt: str, max_tokens: int = 8192) -> dict:
    """Call the LLM and parse the JSON response. Retries once on parse failure."""
    import re

    for attempt in range(2):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3].strip()

        # If response was truncated (hit max_tokens), try to close the JSON
        if response.stop_reason == "max_tokens":
            # Count open braces/brackets and close them
            open_braces = text.count("{") - text.count("}")
            open_brackets = text.count("[") - text.count("]")
            text = text.rstrip(", \n")
            text += "]" * max(0, open_brackets)
            text += "}" * max(0, open_braces)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find the outermost JSON object
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            if attempt == 0:
                print(f"    JSON parse failed, retrying...", file=sys.stderr)
                continue

            raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")


def process_section(client: Anthropic, section_id: str, section_text: str) -> dict:
    """Process a single section through the appropriate LLM."""
    config = SECTION_CONFIGS.get(section_id)
    if not config:
        return {"raw_text": section_text[:1000], "error": f"No config for section: {section_id}"}

    # Truncate if needed
    max_chars = config.get("max_chars", 100_000)
    if len(section_text) > max_chars:
        section_text = section_text[:max_chars] + "\n\n[TRUNCATED]"

    # Build prompt
    if "section_type" in config:
        prompt = config["prompt"].format(text=section_text, section_type=config["section_type"])
    else:
        prompt = config["prompt"].format(text=section_text)

    model = config["model"]
    print(f"  {section_id}: sending {len(section_text):,} chars to {model}...", file=sys.stderr)

    max_tokens = config.get("max_tokens", 8192)
    result = call_llm(client, model, prompt, max_tokens=max_tokens)
    print(f"  {section_id}: done", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Model code classification — assign codes to BS/CF items for the model
# ---------------------------------------------------------------------------

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
    """Flatten nested per-period BS data into items with unique IDs."""
    all_items = {}
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
    """Flatten nested CF JSON into items with unique IDs."""
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
                items.append({"id": f"{section_name}/{key}", "key": key,
                              "section": section_name, "values": period_vals})
            else:
                collect(value, f"{section_name}/{key}")

    collect(op_cf, "operating")
    collect(inv_cf, "investing")
    collect(fin_cf, "financing")
    for top_key, value in cf_data.items():
        if top_key in skip_top or not isinstance(value, dict):
            continue
        period_vals = {p: value[p] for p in periods
                       if p in value and isinstance(value[p], (int, float))}
        if period_vals:
            items.append({"id": f"summary/{top_key}", "key": top_key,
                          "section": "summary", "values": period_vals})
        else:
            collect(value, top_key)
    return items


def _detect_cf_periods(cf_data):
    """Detect period keys from section-first CF data."""
    periods = set()
    def scan(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return
        for v in obj.values():
            if isinstance(v, dict):
                for dk, dv in v.items():
                    if isinstance(dk, str) and len(dk) >= 4 and dk[:4].isdigit() and isinstance(dv, (int, float)):
                        periods.add(dk)
                if not periods:
                    scan(v, depth + 1)
    scan(cf_data)
    return sorted(periods)


def _clean_label(key):
    return key.replace("_", " ").strip().title()


def _classify_with_llm(client, items, code_defs, statement_type):
    """Use LLM to assign model codes to financial line items."""
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
5. Spread items across available catch-all buckets by category.
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
                print(f"    LLM classify retry ({statement_type})...", file=sys.stderr)
    raise ValueError(f"Failed to parse LLM classification for {statement_type}")


def classify_model_codes(client, results):
    """Post-process: assign model codes to BS and CF items.

    Adds '_coded_items' lists to balance_sheet and cash_flows sections.
    Each coded item: {"code": "BS_CASH", "label": "...", "values": {period: val}}.
    """
    # --- Balance Sheet ---
    bs_raw = results.get("balance_sheet", {})
    if bs_raw:
        inner = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))
        periods = sorted([k for k in inner if isinstance(inner.get(k), dict)
                          and len(k) >= 4 and k[:4].isdigit()
                          and not k.lower().endswith("_usd")])
        if periods:
            bs_items = _flatten_bs(inner, periods)
            if bs_items:
                print("  Classifying BS items...", file=sys.stderr)
                mapping = _classify_with_llm(client, bs_items, BS_CODE_DEFS, "Balance Sheet")
                coded = []
                for item in bs_items:
                    code = mapping.get(item["id"])
                    if code and code != "SKIP":
                        coded.append({"code": code, "label": _clean_label(item["key"]),
                                      "values": item["values"]})
                bs_raw["_coded_items"] = coded
                print(f"    {len(coded)} items classified", file=sys.stderr)

    # --- Cash Flows ---
    cf_data = results.get("cash_flows", {})
    if cf_data:
        periods = _detect_cf_periods(cf_data)
        if periods:
            cf_items = _flatten_cf(cf_data, periods)
            if cf_items:
                print("  Classifying CF items...", file=sys.stderr)
                mapping = _classify_with_llm(client, cf_items, CF_CODE_DEFS, "Cash Flow Statement")
                coded = []
                for item in cf_items:
                    code = mapping.get(item["id"])
                    if code and code != "SKIP":
                        coded.append({"code": code, "label": _clean_label(item["key"]),
                                      "values": item["values"]})
                cf_data["_coded_items"] = coded
                print(f"    {len(coded)} items classified", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Structure extracted financial sections into JSON")
    parser.add_argument("sections_dir", help="Directory with extracted section .txt files")
    parser.add_argument("--output", "-o", default=None, help="Output JSON file (default: stdout)")
    parser.add_argument("--sections", nargs="*", default=None,
                        help="Specific sections to process (default: all)")
    args = parser.parse_args()

    # Read manifest
    manifest_path = os.path.join(args.sections_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"Error: No manifest.json in {args.sections_dir}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Determine which sections to process
    sections_to_process = args.sections or list(manifest.keys())

    client = Anthropic()
    results = {}

    for section_id in sections_to_process:
        if section_id.startswith("_"):
            continue  # skip metadata entries like _headcount

        if section_id not in manifest:
            print(f"  WARNING: Section '{section_id}' not in manifest, skipping", file=sys.stderr)
            continue

        info = manifest[section_id]
        if "file" not in info:
            continue  # skip entries without files

        section_file = info["file"]

        if not os.path.exists(section_file):
            print(f"  WARNING: File not found: {section_file}", file=sys.stderr)
            continue

        with open(section_file) as f:
            section_text = f.read()

        try:
            results[section_id] = process_section(client, section_id, section_text)
        except Exception as e:
            print(f"  ERROR processing {section_id}: {e}", file=sys.stderr)
            results[section_id] = {"error": str(e)}

    # Classify BS and CF items with model codes
    print("\nClassifying model codes...", file=sys.stderr)
    classify_model_codes(client, results)

    output = json.dumps(results, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"\nSaved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
