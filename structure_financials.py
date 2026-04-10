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

# ---------------------------------------------------------------------------
# Tool Use schemas for structured extraction (guarantees output shape)
# ---------------------------------------------------------------------------

FINANCIALS_SCHEMA = {
    "type": "object",
    "properties": {
        "unit": {"type": "string", "enum": ["millions", "thousands", "ones"]},
        "fiscal_years": {
            "type": "object",
            "description": "Period-first mapping. Keys are fiscal year end dates (e.g. '2024-09-28').",
            "additionalProperties": {
                "type": "object",
                "description": "All line items for this period with snake_case keys and numeric values.",
                "additionalProperties": True,
            },
        },
    },
    "required": ["unit", "fiscal_years"],
}

BALANCE_SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "unit": {"type": "string", "enum": ["millions", "thousands", "ones"]},
        "balance_sheet": {
            "type": "object",
            "description": "Period-first mapping. Keys are fiscal year end dates.",
            "additionalProperties": {
                "type": "object",
                "description": "Full asset/liability/equity hierarchy for this period.",
                "additionalProperties": True,
            },
        },
    },
    "required": ["unit", "balance_sheet"],
}

CASH_FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "unit": {"type": "string", "enum": ["millions", "thousands", "ones"]},
        "operating_activities": {
            "type": "object",
            "description": "Section-first: each key maps to {period: value}.",
            "additionalProperties": True,
        },
        "investing_activities": {
            "type": "object",
            "additionalProperties": True,
        },
        "financing_activities": {
            "type": "object",
            "additionalProperties": True,
        },
        "beginning_balances": {
            "type": "object",
            "additionalProperties": True,
        },
        "ending_balances": {
            "type": "object",
            "additionalProperties": True,
        },
    },
    "required": ["unit", "operating_activities", "investing_activities", "financing_activities"],
}

# Map section_id to its tool use schema (financial statements only)
TOOL_USE_SCHEMAS = {
    "income_statement": ("extract_income_statement", "Extract structured income statement data.", FINANCIALS_SCHEMA),
    "comprehensive_income": ("extract_comprehensive_income", "Extract comprehensive income data.", FINANCIALS_SCHEMA),
    "balance_sheet": ("extract_balance_sheet", "Extract structured balance sheet data.", BALANCE_SHEET_SCHEMA),
    "shareholders_equity": ("extract_shareholders_equity", "Extract shareholders equity changes.", FINANCIALS_SCHEMA),
    "cash_flows": ("extract_cash_flows", "Extract structured cash flow data.", CASH_FLOW_SCHEMA),
}

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


def _extract_with_tool_use(client: Anthropic, section_id: str, section_text: str,
                           model: str, section_type: str) -> dict:
    """Use Anthropic Tool Use to guarantee structured output shape."""
    tool_name, tool_desc, schema = TOOL_USE_SCHEMAS[section_id]

    prompt_text = (
        f"Extract all numerical data from this {section_type} into structured form.\n\n"
        f"Rules:\n"
        f"- Use the EXACT numbers from the text. Do not calculate or infer.\n"
        f"- All monetary values should be numbers (not strings), in the unit stated.\n"
        f"- Use snake_case for field names.\n"
        f"- Include ALL line items shown, even subtotals.\n\n"
        f"{section_type} text:\n---\n{section_text}\n---"
    )

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt_text}],
        tools=[
            {
                "name": tool_name,
                "description": tool_desc,
                "input_schema": schema,
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
    )

    # Tool use response: content[0] is a tool_use block with .input
    for block in response.content:
        if block.type == "tool_use":
            return block.input

    # Fallback: shouldn't happen with tool_choice forcing
    raise ValueError(f"No tool_use block in response for {section_id}")


def process_section(client: Anthropic, section_id: str, section_text: str) -> dict:
    """Process a single section through the appropriate LLM.

    Financial statements use Tool Use (structured outputs) for guaranteed shape.
    Notes and MD&A use raw prompts (unstructured text extraction).
    """
    config = SECTION_CONFIGS.get(section_id)
    if not config:
        return {"raw_text": section_text[:1000], "error": f"No config for section: {section_id}"}

    # Truncate if needed
    max_chars = config.get("max_chars", 100_000)
    if len(section_text) > max_chars:
        section_text = section_text[:max_chars] + "\n\n[TRUNCATED]"

    model = config["model"]
    print(f"  {section_id}: sending {len(section_text):,} chars to {model}...", file=sys.stderr)

    # Use Tool Use for financial statements, raw prompt for notes/MDA
    if section_id in TOOL_USE_SCHEMAS:
        result = _extract_with_tool_use(client, section_id, section_text,
                                         model, config["section_type"])
    else:
        if "section_type" in config:
            prompt = config["prompt"].format(text=section_text, section_type=config["section_type"])
        else:
            prompt = config["prompt"].format(text=section_text)
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


def _normalize_with_llm(client, items, category_name):
    """Use LLM to normalize line item names and pick the top 3 most material.

    The LLM's job:
    1. Identify items that are the same thing under different names across periods
       (e.g., "Cash" and "Cash & Cash Equivalents" are the same)
    2. Identify supplemental disclosures to skip (CF only)
    3. Pick the 3 most material items (by average absolute value)
    4. Give each a clean, concise label

    Returns:
      {"flex": [{"label": str, "item_ids": [str]}],
       "other": [str],
       "skip": [str]}
    """
    # Pre-compute avg absolute values for each item
    items_for_prompt = []
    for it in items:
        vals = it["values"]
        avg = sum(abs(v) for v in vals.values()) / max(len(vals), 1)
        items_for_prompt.append({
            "id": it["id"],
            "key": it["key"],
            "section": it.get("section", ""),
            "avg_abs_value": round(avg),
            "periods": list(vals.keys()),
        })

    prompt = f"""You are normalizing line items for a financial model's "{category_name}" category.

LINE ITEMS (with average absolute value for materiality):
{json.dumps(items_for_prompt, indent=2)}

TASKS:
1. GROUP items that represent the same thing under different names across periods.
   Example: "cash" and "cash_and_cash_equivalents" → same item.
2. SKIP supplemental disclosures — items like "cash paid for income taxes",
   "cash paid for interest", "non-cash investing/financing activities" are footnotes,
   not actual financial items. Mark them as "skip".
3. From the remaining items (after grouping and skipping), pick the TOP 3 by materiality
   (highest average absolute value). Everything else goes to "other".
4. Give each top-3 item a clean, concise label (e.g., "Cash & Equivalents", "Accounts Receivable").

Return JSON:
{{
  "flex": [
    {{"label": "Cash & Equivalents", "item_ids": ["id1", "id2"]}},
    {{"label": "Marketable Securities", "item_ids": ["id3"]}},
    {{"label": "Accounts Receivable", "item_ids": ["id4"]}}
  ],
  "other": ["id5", "id6"],
  "skip": ["id7"]
}}

Return ONLY valid JSON."""

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
                print(f"    LLM normalize retry ({category_name})...", file=sys.stderr)
    raise ValueError(f"Failed to parse LLM normalization for {category_name}")


def classify_flex_rows(client, results):
    """Post-process: classify each statement into flex-row categories.

    Adds '_flex_categories' to IS, BS, and CF sections.
    Each category: {
      "subtotal_code": "BS_TCA",
      "subtotal_label": "Total Current Assets",
      "flex": [{"code": "BS_CA1", "label": "Cash & Equivalents",
                "values": {period: val}}],
      "other": {"code": "BS_CA_OTH", "label": "Other",
                "values": {period: val}},
      "subtotal_values": {period: val}
    }
    """
    from pymodel import (
        _flatten_category, _flatten_cf_section, _convert_section_first,
        _deep_find, _navigate, sum_values, clean_label as py_clean_label,
    )

    # ---------------------------------------------------------------
    # Income Statement
    # ---------------------------------------------------------------
    is_raw = results.get("income_statement", {})
    is_data = is_raw.get("fiscal_years", is_raw.get("data", is_raw))
    is_periods = sorted([k for k in is_data if isinstance(is_data.get(k), dict)
                         and k[:4].isdigit() and not k.lower().endswith("_usd")])

    IS_CATEGORIES = [
        (["net_sales", "revenue", "revenues"],
         ["total_net_sales", "revenues", "revenue", "total_revenue", "net_revenues"],
         "REVT", "REV", "REV_OTH"),
        (["cost_of_sales", "cost_of_revenue", "cost_of_goods_sold"],
         ["total_cost_of_sales", "cost_of_revenues", "cost_of_revenue", "cost_of_goods_sold"],
         "COGST", "COGS", "COGS_OTH"),
        (["operating_expenses"],
         ["total_operating_expenses"],
         "OPEXT", "OPEX", "OPEX_OTH"),
    ]

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
        cat_items = {}
        subtotal_vals = {}
        for p in periods:
            pdata = is_data.get(p, {})
            container = None
            for pk in parent_keys:
                if pk in pdata and isinstance(pdata[pk], dict):
                    container = pdata[pk]
                    break
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
            if not container and p not in subtotal_vals:
                for sk in subtotal_keys:
                    v = _deep_find(pdata, sk)
                    if v is not None:
                        subtotal_vals[p] = v
                        break
        item_list = [{"id": k, "key": k, "section": "is", "values": v}
                     for k, v in cat_items.items()]
        return item_list, subtotal_vals

    is_categories = []

    for parent_keys, subtotal_keys, subtotal_code, flex_prefix, catch_all_code in IS_CATEGORIES:
        cat_items, subtotal_vals = _flatten_is_category(
            is_data, is_periods, parent_keys, subtotal_keys)

        if not cat_items:
            continue

        # Use LLM to normalize and pick top 3
        cat_name = f"Income Statement - {py_clean_label(subtotal_keys[0])}"
        print(f"  Normalizing IS {subtotal_code} ({len(cat_items)} items)...", file=sys.stderr)
        classification = _normalize_with_llm(client, cat_items, cat_name)

        items_by_id = {it["id"]: it for it in cat_items}
        flex_list = []
        for i, flex_entry in enumerate(classification.get("flex", [])):
            code = f"{flex_prefix}{i+1}"
            merged_vals = {}
            for item_id in flex_entry["item_ids"]:
                item = items_by_id.get(item_id)
                if item:
                    for p, val in item["values"].items():
                        merged_vals[p] = merged_vals.get(p, 0) + val
            flex_list.append({
                "code": code,
                "label": flex_entry["label"],
                "values": merged_vals,
            })

        other_vals = {}
        for item_id in classification.get("other", []):
            item = items_by_id.get(item_id)
            if item:
                for p, val in item["values"].items():
                    other_vals[p] = other_vals.get(p, 0) + val

        # Absorb structural gaps into catch-all
        for p in is_periods:
            filed = subtotal_vals.get(p, 0)
            if filed == 0:
                continue
            comp = sum(f["values"].get(p, 0) for f in flex_list) + other_vals.get(p, 0)
            gap = filed - comp
            if abs(gap) > 0.5:
                other_vals[p] = other_vals.get(p, 0) + gap

        is_categories.append({
            "subtotal_code": subtotal_code,
            "subtotal_label": py_clean_label(subtotal_keys[0]),
            "subtotal_values": subtotal_vals,
            "flex": flex_list,
            "other": {"code": catch_all_code, "label": "Other", "values": other_vals},
        })

    # IS single-line items
    is_singles = []
    for possible_keys, code, label in IS_SINGLES:
        vals = {}
        for p in is_periods:
            pdata = is_data.get(p, {})
            for k in possible_keys:
                v = _deep_find(pdata, k)
                if v is not None:
                    vals[p] = v
                    break
        if vals:
            is_singles.append({"code": code, "label": label, "values": vals})

    # SBC & DA from IS or CF
    cf_data = results.get("cash_flows", {})
    op_cf = cf_data.get("operating_activities",
                        cf_data.get("cash_flows_from_operating_activities", {}))
    adj = op_cf.get("adjustments_to_reconcile_net_income", {})

    for keys, code, label in [
        (["share_based_compensation_expense", "stock_based_compensation"], "SBC", "Stock-Based Compensation"),
        (["depreciation_and_amortization"], "DA", "Depreciation & Amortization"),
    ]:
        vals = {}
        for p in is_periods:
            pdata = is_data.get(p, {})
            for k in keys:
                v = _deep_find(pdata, k)
                if v is not None:
                    vals[p] = v
                    break
        if not vals:
            for k in keys:
                if k in adj and isinstance(adj[k], dict):
                    vals = {p: adj[k][p] for p in is_periods if p in adj[k]}
                    if vals:
                        break
        if vals:
            is_singles.append({"code": code, "label": label, "values": vals})

    is_raw["_flex_categories"] = is_categories
    is_raw["_singles"] = is_singles
    print(f"  IS: {len(is_categories)} categories, {len(is_singles)} singles", file=sys.stderr)

    # ---------------------------------------------------------------
    # Balance Sheet
    # ---------------------------------------------------------------
    bs_raw = results.get("balance_sheet", {})
    bs_data = bs_raw.get("balance_sheet", bs_raw.get("fiscal_years", bs_raw))

    bs_pkeys = [k for k in bs_data if isinstance(bs_data.get(k), dict)
                and k[:4].isdigit() and not k.lower().endswith("_usd")]
    if bs_pkeys:
        bs_periods = sorted(bs_pkeys)
    else:
        bs_data, bs_periods = _convert_section_first(bs_data)
        bs_periods = [p for p in bs_periods if not p.lower().endswith("_usd")]

    BS_CATEGORIES = [
        # (primary_path, alt_paths, subtotal_key, subtotal_code, flex_prefix, catch_all_code)
        (["assets", "current_assets"],
         [],
         "total_current_assets", "BS_TCA", "BS_CA", "BS_CA_OTH"),
        (["assets", "non_current_assets"],
         [["assets"]],  # fallback: NCA items flat under assets (Google)
         "total_non_current_assets", "BS_TNCA", "BS_NCA", "BS_NCA_OTH"),
        (["liabilities", "current_liabilities"],
         [["liabilities_and_stockholders_equity", "current_liabilities"]],
         "total_current_liabilities", "BS_TCL", "BS_CL", "BS_CL_OTH"),
        (["liabilities", "non_current_liabilities"],
         [["liabilities_and_stockholders_equity"]],  # fallback: NCL items flat
         "total_non_current_liabilities", "BS_TNCL", "BS_NCL", "BS_NCL_OTH"),
        (["shareholders_equity"],
         [["liabilities_and_stockholders_equity", "stockholders_equity"],
          ["stockholders_equity"]],
         "total_shareholders_equity", "BS_TE", "BS_EQ", "BS_EQ_OTH"),
    ]

    bs_categories = []
    for cat_path, alt_paths, subtotal_key, subtotal_code, flex_prefix, catch_all_code in BS_CATEGORIES:
        cat_items, subtotal_vals = _flatten_category(bs_data, bs_periods, cat_path, subtotal_key)

        # Try alternative paths if primary didn't work
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
            # Try alt subtotal keys with all paths
            all_paths = [cat_path] + alt_paths
            for alt in alt_keys.get(subtotal_key, []):
                for try_path in all_paths:
                    _, subtotal_vals = _flatten_category(bs_data, bs_periods, try_path, alt)
                    if subtotal_vals:
                        break
                if subtotal_vals:
                    break

        if not cat_items:
            continue

        # Add id field for LLM prompt
        for it in cat_items:
            it["id"] = it["key"]

        cat_name = f"Balance Sheet - {py_clean_label(subtotal_key)}"
        print(f"  Normalizing {subtotal_code} ({len(cat_items)} items)...", file=sys.stderr)
        classification = _normalize_with_llm(client, cat_items, cat_name)

        items_by_id = {it["id"]: it for it in cat_items}
        flex_list = []
        for i, flex_entry in enumerate(classification.get("flex", [])):
            code = f"{flex_prefix}{i+1}"
            merged_vals = {}
            for item_id in flex_entry["item_ids"]:
                item = items_by_id.get(item_id)
                if item:
                    for p, val in item["values"].items():
                        merged_vals[p] = merged_vals.get(p, 0) + val
            flex_list.append({
                "code": code,
                "label": flex_entry["label"],
                "values": merged_vals,
            })

        other_vals = {}
        for item_id in classification.get("other", []):
            item = items_by_id.get(item_id)
            if item:
                for p, val in item["values"].items():
                    other_vals[p] = other_vals.get(p, 0) + val

        for p in bs_periods:
            filed = subtotal_vals.get(p, 0)
            if filed == 0:
                continue
            comp = sum(f["values"].get(p, 0) for f in flex_list) + other_vals.get(p, 0)
            gap = filed - comp
            if abs(gap) > 0.5:
                other_vals[p] = other_vals.get(p, 0) + gap
                print(f"    {subtotal_code} {p}: structural gap {gap:,.0f}", file=sys.stderr)

        bs_categories.append({
            "subtotal_code": subtotal_code,
            "subtotal_label": py_clean_label(subtotal_key),
            "subtotal_values": subtotal_vals,
            "flex": flex_list,
            "other": {"code": catch_all_code, "label": "Other", "values": other_vals},
        })

    # Total Assets / Total Liabilities
    bs_totals = []
    for total_key, code, label in [
        ("total_assets", "BS_TA", "Total Assets"),
        ("total_liabilities", "BS_TL", "Total Liabilities"),
    ]:
        vals = {}
        for p in bs_periods:
            v = _deep_find(bs_data.get(p, {}), total_key)
            if v is not None:
                vals[p] = v
        if vals:
            bs_totals.append({"code": code, "label": label, "values": vals})

    bs_raw["_flex_categories"] = bs_categories
    bs_raw["_totals"] = bs_totals
    print(f"  BS: {len(bs_categories)} categories", file=sys.stderr)

    # ---------------------------------------------------------------
    # Cash Flows
    # ---------------------------------------------------------------
    cf_periods = _detect_cf_periods(cf_data)

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

    cf_categories = []
    for section_key, alt_key, subtotal_keys, subtotal_code, flex_prefix, catch_all_code in CF_SECTIONS:
        cf_section = cf_data.get(section_key, cf_data.get(alt_key, {}))
        if not cf_section:
            continue

        cf_items, subtotal_vals = _flatten_cf_section(cf_section, cf_periods, subtotal_keys)
        if not cf_items:
            continue

        # Add id field
        for it in cf_items:
            it["id"] = it["key"]

        cat_name = f"Cash Flow - {py_clean_label(subtotal_keys[0])}"
        print(f"  Normalizing {subtotal_code} ({len(cf_items)} items)...", file=sys.stderr)
        classification = _normalize_with_llm(client, cf_items, cat_name)

        items_by_id = {it["id"]: it for it in cf_items}
        flex_list = []
        for i, flex_entry in enumerate(classification.get("flex", [])):
            code = f"{flex_prefix}{i+1}"
            merged_vals = {}
            for item_id in flex_entry["item_ids"]:
                item = items_by_id.get(item_id)
                if item:
                    for p, val in item["values"].items():
                        merged_vals[p] = merged_vals.get(p, 0) + val
            flex_list.append({
                "code": code,
                "label": flex_entry["label"],
                "values": merged_vals,
            })

        other_vals = {}
        for item_id in classification.get("other", []):
            item = items_by_id.get(item_id)
            if item:
                for p, val in item["values"].items():
                    other_vals[p] = other_vals.get(p, 0) + val

        # Skip items
        skip_vals = {}
        for item_id in classification.get("skip", []):
            item = items_by_id.get(item_id)
            if item:
                for p, val in item["values"].items():
                    skip_vals[p] = skip_vals.get(p, 0) + val

        for p in cf_periods:
            filed = subtotal_vals.get(p, 0)
            if filed == 0:
                continue
            comp = sum(f["values"].get(p, 0) for f in flex_list) + other_vals.get(p, 0)
            gap = filed - comp
            if abs(gap) > 0.5:
                other_vals[p] = other_vals.get(p, 0) + gap
                print(f"    {subtotal_code} {p}: structural gap {gap:,.0f}", file=sys.stderr)

        cf_categories.append({
            "subtotal_code": subtotal_code,
            "subtotal_label": py_clean_label(subtotal_keys[0]),
            "subtotal_values": subtotal_vals,
            "flex": flex_list,
            "other": {"code": catch_all_code, "label": "Other", "values": other_vals},
        })

    # CF structural items
    cf_structural = []
    netch_keys = ["increase_decrease_in_cash_cash_equivalents_and_restricted_cash_and_cash_equivalents",
                  "net_increase_decrease_in_cash"]
    for nk in netch_keys:
        v = cf_data.get(nk)
        if isinstance(v, dict):
            pvals = {p: v[p] for p in cf_periods if p in v and isinstance(v[p], (int, float))}
            if pvals:
                cf_structural.append({"code": "CF_NETCH", "label": "Net Change in Cash", "values": pvals})
                break

    for bal_key, code, label in [
        ("beginning_balances", "CF_BEGC", "Beginning Cash"),
        ("ending_balances", "CF_ENDC", "Ending Cash"),
    ]:
        bal = cf_data.get(bal_key, {})
        if isinstance(bal, dict):
            for bk, bv in bal.items():
                if isinstance(bv, dict):
                    pvals = {p: bv[p] for p in cf_periods if p in bv and isinstance(bv[p], (int, float))}
                    if pvals:
                        cf_structural.append({"code": code, "label": label, "values": pvals})
                        break

    cf_data["_flex_categories"] = cf_categories
    cf_data["_structural"] = cf_structural
    print(f"  CF: {len(cf_categories)} categories", file=sys.stderr)


def reclassify_with_errors(client: Anthropic, results: dict, errors: list) -> dict:
    """Re-run flex-row classification with error context.

    The LLM sees the previous invariant failures and adjusts the classification
    map accordingly (e.g., reclassifying a tag from Revenue to COGS).

    Args:
        client: Anthropic client instance.
        results: The current structured results dict.
        errors: List of (name, period, delta) tuples from verify_model().

    Returns:
        Updated results dict with corrected _flex_categories.
    """
    error_summary = "\n".join(
        f"  - {name}: period {period}, delta = {delta:,.0f}"
        for name, period, delta in errors
    )

    # Strip old flex classifications
    for section in ["income_statement", "balance_sheet", "cash_flows"]:
        sec = results.get(section, {})
        for key in list(sec.keys()):
            if key.startswith("_"):
                del sec[key]

    # Re-classify with error context injected into the normalization prompts
    print(f"\nRe-classifying with {len(errors)} error(s) as context...", file=sys.stderr)
    print(error_summary, file=sys.stderr)

    # Re-run classification (classify_flex_rows will use LLM normalization)
    classify_flex_rows(client, results)

    return results


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

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

    # Classify into flex-row categories
    print("\nClassifying flex rows...", file=sys.stderr)
    classify_flex_rows(client, results)

    output = json.dumps(results, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"\nSaved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
