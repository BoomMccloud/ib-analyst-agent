"""
Agent 3: Financial Modeler
==========================
Takes structured financial data + MD&A from Agent 2 and proposes a bottom-up
financial model with revenue drivers, expense assumptions, and cash flow items.

Usage:
  python agent3_modeler.py --financials /tmp/aapl_financials.json --mda /tmp/aapl_mda.json -o model.json
  python agent3_modeler.py --sections-dir /tmp/aapl_sections --structured /tmp/aapl_structured.json -o model.json

Requires: ANTHROPIC_API_KEY env var
"""

import argparse
import json
import sys

from anthropic import Anthropic

from llm_utils import parse_json_response

MODEL = "claude-sonnet-4-6"

MODELER_PROMPT = """\
You are a senior equity research analyst building a financial model for {company}.

You have been given:
1. **Structured financial statements** (income statement, balance sheet, cash flows) for the last several years
2. **MD&A analysis** with revenue segments, expense discussion, key metrics, and capital allocation

Your task: Design a bottom-up financial model specification that can be used to forecast the next 5 years.

## CRITICAL: Source Citations
Every forecast assumption MUST include a "source" field citing where the data or rationale comes from:
- Quote specific passages from the MD&A (e.g., "MD&A: 'Services grew 13% driven by App Store and advertising'")
- Reference specific notes (e.g., "Note 5: Revenue by segment shows Cloud at $33.1B")
- Reference historical trends (e.g., "Historical: R&D as % of revenue averaged 15.2% over 3 years")
- If an assumption is an analyst judgment, say so (e.g., "Assumption: based on industry benchmarks")

## Requirements

### Revenue Model
For each meaningful revenue segment:
- Identify the **driver formula** (e.g., "subscribers × ARPU", "GMV × take_rate", "units × ASP")
- If specific drivers aren't available from the MD&A, use "segment_revenue × (1 + growth_rate)"
- Provide **historical values** for each driver where possible
- Suggest **assumption ranges** for forecasting (base/bull/bear)
- Include **"source"** field citing MD&A or notes for each driver assumption

### Expense Model
For each major expense line (R&D, S&M, G&A, COGS):

**If headcount breakdown by function (R&D, S&M, G&A) is available:**
- Use headcount × cost_per_employee as the primary driver for each line
- Show historical headcount per function, cost per employee, and trends
- For forecast: project headcount growth per function + cost-per-employee inflation
- Add any non-headcount expense components separately (e.g., content costs, ad spend, depreciation)

**If only TOTAL headcount is available (more common):**
- Model each expense line as % of revenue (with historical trend) for the primary forecast
- Add a **"headcount validation" section** that acts as a sanity check:
  - Calculate: total opex (R&D + S&M + G&A) / total headcount = fully loaded cost per employee
  - Show this metric historically
  - For the forecast years, divide the forecasted total opex by the same fully-loaded cost (with inflation) to derive the **implied headcount**
  - This implied headcount should be sanity-checked: does the growth rate make sense given the company's hiring trends?
  - Flag if implied headcount growth diverges significantly from historical trends

**For COGS:** Model separately — typically not headcount-driven. If product vs. services split exists, model each as % of respective segment revenue.
- Note non-headcount components from the MD&A (logistics, content costs, depreciation, etc.)

### Cash Flow Model
For each material cash flow item:
- Capex: as % of revenue or absolute
- Working capital: days sales outstanding, days payable, inventory turns
- Dividends: payout ratio or absolute per share
- Buybacks: historical pattern
- Debt: maturity schedule if available

### Output Format
Return ONLY valid JSON in this exact structure:
{{
  "company": "{company}",
  "currency": "USD or RMB etc.",
  "unit": "millions",
  "fiscal_year_end": "month-day",
  "historical_periods": ["2022", "2023", "2024"],

  "revenue_model": {{
    "segments": [
      {{
        "name": "segment name",
        "driver_formula": "units × ASP",
        "drivers": {{
          "driver_name": {{
            "historical": {{"2022": val, "2023": val, "2024": val}},
            "unit": "millions/dollars/percentage/etc",
            "forecast_assumption": {{
              "method": "growth_rate or absolute",
              "base_case": "description + numbers",
              "bull_case": "description + numbers",
              "bear_case": "description + numbers",
              "source": "MD&A quote or Note reference justifying this assumption"
            }}
          }}
        }},
        "historical_revenue": {{"2022": val, "2023": val, "2024": val}},
        "source": "where this segment data comes from (e.g., Note 5, MD&A revenue discussion)"
      }}
    ],
    "total_revenue_historical": {{"2022": val, "2023": val, "2024": val}}
  }},

  "expense_model": {{
    "total_headcount": {{
      "historical": {{"2022": val, "2023": val, "2024": val}},
      "source": "where the headcount number came from"
    }},
    "cogs": {{
      "method": "split_by_segment or pct_of_revenue",
      "components": ["list non-headcount components: logistics, content, depreciation, etc."],
      "historical": {{"2022": val, "2023": val, "2024": val}},
      "historical_pct_of_revenue": {{"2022": pct, "2023": pct, "2024": pct}},
      "forecast_assumption": "description",
      "source": "MD&A or Notes reference"
    }},
    "research_and_development": {{
      "method": "headcount_driven or pct_of_revenue",
      "historical": {{}},
      "historical_pct_of_revenue": {{}},
      "cost_per_employee": {{"2022": val, "2023": val, "2024": val}},
      "non_headcount_components": ["any other components noted in MD&A"],
      "forecast_assumption": "description",
      "source": "MD&A or Notes reference"
    }},
    "selling_general_and_administrative": {{
      "method": "headcount_driven or pct_of_revenue",
      "historical": {{}},
      "historical_pct_of_revenue": {{}},
      "cost_per_employee": {{"2022": val, "2023": val, "2024": val}},
      "non_headcount_components": ["any other components noted in MD&A"],
      "forecast_assumption": "description",
      "source": "MD&A or Notes reference"
    }},
    "headcount_validation": {{
      "total_headcount_historical": {{"2022": val, "2023": val, "2024": val}},
      "total_opex_historical": {{"2022": val, "2023": val, "2024": val}},
      "fully_loaded_cost_per_employee": {{"2022": val, "2023": val, "2024": val}},
      "note": "For forecast years, divide projected total opex by this cost (with ~3% annual inflation) to get implied headcount. Check if implied headcount growth is reasonable."
    }}
  }},

  "other_income_expense": {{
    "method": "description of how to model",
    "historical": {{}}
  }},

  "tax_model": {{
    "method": "effective_tax_rate",
    "historical_effective_rate": {{}},
    "forecast_assumption": "description"
  }},

  "cash_flow_model": {{
    "depreciation_and_amortization": {{
      "method": "pct_of_revenue or pct_of_ppe",
      "historical": {{}},
      "forecast_assumption": "description"
    }},
    "capex": {{
      "method": "pct_of_revenue",
      "historical": {{}},
      "historical_pct_of_revenue": {{}},
      "forecast_assumption": "description"
    }},
    "working_capital": {{
      "method": "description",
      "historical_metrics": {{}}
    }},
    "share_based_compensation": {{
      "method": "pct_of_revenue",
      "historical": {{}},
      "forecast_assumption": "description"
    }},
    "dividends": {{
      "method": "per_share or payout_ratio",
      "historical": {{}},
      "forecast_assumption": "description"
    }},
    "buybacks": {{
      "method": "description",
      "historical": {{}}
    }}
  }},

  "balance_sheet_drivers": {{
    "key_items": ["list of key balance sheet items to forecast"],
    "method": "description of approach"
  }},

  "key_assumptions_summary": [
    "Top 5-10 most impactful assumptions that drive the model"
  ]
}}

## Input Data

### Structured Financial Statements
{financials_json}

### MD&A Analysis
{mda_json}
"""


def build_model(company: str, financials: dict, mda: dict) -> dict:
    """Send financials + MD&A to the LLM and get a model specification."""
    client = Anthropic()

    financials_json = json.dumps(financials, indent=2)
    mda_json = json.dumps(mda, indent=2)

    prompt = MODELER_PROMPT.format(
        company=company,
        financials_json=financials_json,
        mda_json=mda_json,
    )

    print(f"Sending {len(prompt):,} chars to {MODEL}...", file=sys.stderr)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16384,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()

    if response.stop_reason == "max_tokens":
        print("  WARNING: Response truncated, attempting to close JSON...", file=sys.stderr)

    try:
        return parse_json_response(text, response.stop_reason)
    except ValueError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description="Agent 3: Build financial model from structured data")
    parser.add_argument("--sections-dir", help="Directory with extracted sections (from extract_sections.py)")
    parser.add_argument("--structured", help="Structured financials JSON (from structure_financials.py)")
    parser.add_argument("--company", default=None, help="Company name (auto-detected if not provided)")
    parser.add_argument("-o", "--output", default=None, help="Output file (default: stdout)")
    args = parser.parse_args()

    # Load structured data
    if args.structured:
        with open(args.structured) as f:
            all_data = json.load(f)
    else:
        print("Error: --structured is required", file=sys.stderr)
        sys.exit(1)

    # Separate financials from MD&A
    financial_keys = ["income_statement", "comprehensive_income", "balance_sheet",
                      "shareholders_equity", "cash_flows"]
    financials = {k: v for k, v in all_data.items() if k in financial_keys}
    mda = all_data.get("mda", {})
    notes = all_data.get("notes", {})

    # Include notes revenue/segment data in the MDA if available
    if notes and isinstance(notes, dict):
        for key in ["revenue_breakdown", "segments", "revenue_recognition", "geographic_data"]:
            if key in notes and key not in mda:
                mda[f"notes_{key}"] = notes[key]

    # Include headcount data if available (from manifest)
    headcount = all_data.get("_headcount")
    if headcount:
        mda["headcount"] = headcount
        print(f"  Headcount data: {headcount}", file=sys.stderr)

    # If sections-dir provided, also check manifest for headcount
    if args.sections_dir:
        import os
        manifest_path = os.path.join(args.sections_dir, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
            if "_headcount" in manifest:
                mda["headcount"] = manifest["_headcount"]
                print(f"  Headcount from manifest: {manifest['_headcount']}", file=sys.stderr)

    company = args.company or "the company"

    print(f"Building model for {company}...", file=sys.stderr)
    print(f"  Financial statements: {list(financials.keys())}", file=sys.stderr)
    print(f"  MD&A keys: {list(mda.keys()) if isinstance(mda, dict) else 'N/A'}", file=sys.stderr)

    model = build_model(company, financials, mda)

    output = json.dumps(model, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"\nSaved model to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
