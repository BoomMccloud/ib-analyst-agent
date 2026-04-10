# Phase 1 Implementation Guide: The Physics Engine

This guide walks through the implementation of **Phase 1** of the decoupled SEC pipeline. Our goal is to establish a mathematically perfect historical baseline without any UI/Google Sheets logic getting in the way.

## 1. Delete Obsolete Files

The old templating and 40-code architecture are dead. Run the following commands to delete the obsolete files from the project root:

```bash
rm agent4_spreadsheet.py
rm create_template.py
rm template_row_map.json
rm diagnose_model.py
rm build_model_sheet.py
```

## 2. Commit Test Fixture

Before writing any code, commit a real structured output fixture so tests can run without network access or API keys.

1. Run the existing pipeline on Apple to generate a structured JSON:
   ```bash
   python structure_financials.py ./sections -o tests/fixtures/aapl_structured.json
   ```
2. Verify it contains IS, BS, and CF data across multiple fiscal years.
3. Commit it to `tests/fixtures/aapl_structured.json`.

All tests in this phase load from this fixture — no live API calls.

## 3. Refactor `pymodel.py` (The Mathematical Engine)

`pymodel.py` currently handles a lot of logic. We need to introduce the "enforce-by-construction" API to make it impossible to violate fundamental accounting invariants (like `Total Assets = Current Assets + Non-Current Assets`).

### 3a. Update `load_filing()` Input Contract

`load_filing()` must be updated to accept two separate inputs:
- `raw_xbrl_facts.json` — raw XBRL tag→value dictionary from `extract_sections.py`
- `xbrl_mapping.json` — LLM classification map from `structure_financials.py`

Python applies the classification map to the raw numbers to populate the standard model. This separates "what numbers exist" (deterministic XBRL) from "what do they mean" (LLM judgment), which is critical for the agentic retry loop in Step 5.

### 3b. Add the Tautological API

Add these helper functions directly into `pymodel.py`. These enforce that derived values (like catch-alls or totals) are always calculated from their components.

```python
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
    # NOTE: TA == TL + TE is a real check, handled elsewhere

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
```

*(Note: Ensure you import `set_v` or define it if it's missing/named differently in your codebase. Currently, `pymodel.py` uses something similar to populate the underlying data dictionary.)*

### 3c. Refactor `verify_model()`

Because the tautological API enforces things like `TCA + TNCA = TA` automatically, `verify_model()` should be reduced down to just the 5 "Real Checks". 

Rewrite `verify_model(m)` to strictly verify:
1. `BS_TA == BS_TL + BS_TE`
2. `CF_ENDC == BS_CASH`
3. `INC_NET` (Income Statement) == `INC_NET` (Cash Flow)
4. `D&A` (Income Statement) == `D&A` (Cash Flow)
5. `SBC` (Income Statement) == `SBC` (Cash Flow)

Return a list of errors. If the list is empty, the model perfectly balances.

### 3d. Add the `--checkpoint` Flag

In the `main()` block of `pymodel.py`, we want to be able to run Phase 1 in isolation. Add a `--checkpoint` argument to `argparse`.

```python
    parser.add_argument("--checkpoint", action="store_true", help="Run historical baseline check only")
    # ...
    
    if args.checkpoint:
        print("Running in checkpoint mode. Verifying historical baseline...", file=sys.stderr)
        errors = verify_model(m)
        if errors:
            print("Invariant failures detected in historical data. Aborting.")
            # Print errors...
            sys.exit(1)
        
        # Save perfect baseline to disk
        with open("historical_baseline.json", "w") as f:
            json.dump(m, f, indent=2)
        print("Successfully wrote historical_baseline.json")
        sys.exit(0)
```

## 4. Implement Tests (`tests/test_model_historical.py`)

Create a new test file: `tests/test_model_historical.py`. All tests use the fixture from Step 2 — no network access required.

```python
import pytest
from pymodel import (
    set_category, set_is_cascade, set_bs_totals, set_cf_totals, set_cf_cash,
    verify_model, load_filing, get_v
)

FIXTURE = "tests/fixtures/aapl_structured.json"
```

### Required Tests

The following tests must all be implemented. They map directly to the spec's Layer 1 test table.

| Test | What it checks |
|------|---------------|
| `test_load_filing_periods` | `load_filing()` returns only periods with IS+BS+CF data; drops incomplete years |
| `test_load_filing_categories` | Every category has `subtotal_code`, `flex_codes`, `catch_all_code` |
| `test_set_category_computes_catchall` | `set_category()` always produces `catch_all = subtotal - sum(flex)` — tautological by construction |
| `test_is_cascade_computes_gp_opinc_ni` | `set_is_cascade()` produces correct GP, OPINC, EBT, INC_NET |
| `test_bs_balance` | **Real check:** `TA == TL + TE` for all historical periods |
| `test_cash_link` | **Real check:** `CF_ENDC == BS_CASH` for all historical periods |
| `test_ni_link` | **Real check:** IS `INC_NET` == CF net income for all historical periods |
| `test_verify_model_zero_errors` | `verify_model()` returns empty list on a balanced model (only real checks, no tautological noise) |
| `test_preclassified_matches_fallback` | If fixture has `_flex_categories`, loading via preclassified path produces same model as fallback path |

### Example Test

```python
def test_is_cascade_computes_gp_opinc_ni():
    model = {}  # mock model structure
    period = "2024"
    set_is_cascade(model, period, revt=100, cogst=40, opext=30, inc_o=5, tax=10)
    
    assert get_v(model, "GP", period) == 60       # 100 - 40
    assert get_v(model, "OPINC", period) == 30     # 60 - 30
    assert get_v(model, "INC_NET", period) == 25   # (30 + 5) - 10

def test_verify_model_zero_errors():
    model = load_filing(FIXTURE)
    errors = verify_model(model)
    assert errors == [], f"Invariant failures: {errors}"
```

### Tolerance

All real checks must pass within a 0.5 rounding tolerance (use `abs(a - b) < 0.5`).

## 5. Upgrade `structure_financials.py` to Tool Use

Currently, `structure_financials.py` uses raw strings like `FINANCIAL_STATEMENT_PROMPT` to beg the LLM to return JSON. This is brittle. We want to use Anthropic's Tool Use (Structured Outputs) to guarantee the shape.

### 5a. Define Inline `input_schema` Dicts

Use inline JSON Schema dicts directly — no Pydantic dependency. This keeps the schema co-located with the tool call and avoids an external `schemas.py` file (deferred until 3+ consumers exist).

```python
FINANCIALS_SCHEMA = {
    "type": "object",
    "properties": {
        "unit": {"type": "string", "enum": ["millions", "thousands", "ones"]},
        "fiscal_years": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "revenue": {"type": "number"},
                    "cost_of_revenue": {"type": "number"},
                    # ... other fields ...
                },
                "required": ["revenue", "cost_of_revenue"]
            }
        }
    },
    "required": ["unit", "fiscal_years"]
}
```

### 5b. Update the LLM Call

Modify the Anthropic API call to use the `tools` parameter and force the model to use it.

```python
response = client.messages.create(
    model=SONNET,
    messages=[{"role": "user", "content": prompt_text}],
    tools=[
        {
            "name": "extract_financials",
            "description": "Extract structured financial data.",
            "input_schema": FINANCIALS_SCHEMA
        }
    ],
    tool_choice={"type": "tool", "name": "extract_financials"}
)

# The response content is guaranteed to match the schema
extracted_data = response.content[0].input
```

## 6. Implement the Agentic Retry Loop

The `--checkpoint` path must include an agentic correction loop. If `verify_model()` reports invariant failures, the exact errors are fed back to `structure_financials.py` to adjust the classification map (`xbrl_mapping.json`). This loop runs a maximum of 3 iterations — if invariants still fail after 3 retries, abort with the remaining errors. Do not loop indefinitely.

```python
MAX_RETRIES = 3

for attempt in range(1, MAX_RETRIES + 1):
    model = load_filing(raw_xbrl_facts, xbrl_mapping)
    errors = verify_model(model)
    
    if not errors:
        break
    
    if attempt == MAX_RETRIES:
        print(f"Still {len(errors)} invariant failures after {MAX_RETRIES} attempts. Aborting.")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    
    print(f"Attempt {attempt}: {len(errors)} failures. Feeding errors back to structure_financials...")
    # Re-run structure_financials with error context to produce a corrected xbrl_mapping
    xbrl_mapping = reclassify_with_errors(xbrl_tags, errors)
```

The key idea: `structure_financials.py` needs an entry point (or the prompt needs augmentation) that accepts the previous errors and adjusts the classification. The LLM sees "you mapped tag X to Revenue, but the BS doesn't balance — reconsider."

## Review & Exit Criteria

Before moving on to Phase 2:
1. Ensure the 5 obsolete files are completely deleted.
2. `tests/fixtures/aapl_structured.json` is committed to the repo.
3. Run `pytest tests/test_model_historical.py` — all 9 tests must pass with zero mathematical errors (all 5 real invariants pass, tolerance < 0.5).
4. Run `python pymodel.py --checkpoint` with `raw_xbrl_facts.json` and `xbrl_mapping.json` as inputs. It must:
   - Run the agentic retry loop (up to 3 attempts) if invariants fail.
   - Generate `historical_baseline.json` and exit with 0 on success.
   - `historical_baseline.json` is structurally valid and contains no unmapped flex items.