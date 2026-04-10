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

from llm_utils import call_llm, parse_json_response
from financial_utils import (
    BS_CODE_DEFS,
    CF_CODE_DEFS,
    flatten_bs,
    flatten_cf,
    clean_label,
)

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
            bs_items = flatten_bs(inner, periods)
            if bs_items:
                print("  Classifying BS items...", file=sys.stderr)
                mapping = _classify_with_llm(client, bs_items, BS_CODE_DEFS, "Balance Sheet")
                coded = []
                for item in bs_items:
                    code = mapping.get(item["id"])
                    if code and code != "SKIP":
                        coded.append({"code": code, "label": clean_label(item["key"]),
                                      "values": item["values"]})
                bs_raw["_coded_items"] = coded
                print(f"    {len(coded)} items classified", file=sys.stderr)

    # --- Cash Flows ---
    cf_data = results.get("cash_flows", {})
    if cf_data:
        periods = _detect_cf_periods(cf_data)
        if periods:
            cf_items = flatten_cf(cf_data, periods)
            if cf_items:
                print("  Classifying CF items...", file=sys.stderr)
                mapping = _classify_with_llm(client, cf_items, CF_CODE_DEFS, "Cash Flow Statement")
                coded = []
                for item in cf_items:
                    code = mapping.get(item["id"])
                    if code and code != "SKIP":
                        coded.append({"code": code, "label": clean_label(item["key"]),
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
