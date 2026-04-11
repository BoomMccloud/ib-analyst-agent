# SEC Pipeline: Decoupled 2-Phase State Machine Specification

## 1. File Changes & Operations

### Files to DELETE
* `agent4_spreadsheet.py` (Old template-based spreadsheet writer)
* `create_template.py` (Used for old static templates)
* `template_row_map.json` (Used for old static templates)
* `diagnose_model.py` (Obsolete; validation is in `pymodel.py`)
* `build_model_sheet.py` (Old 40-code architecture; superseded by `pymodel.py` flex-row approach)
* `legacy_pymodel.py` (Obsolete; flat dict models replaced by tree-based modeling)
* `extract_sections.py` (Obsolete; legacy LLM-based extraction path removed)
* `structure_financials.py` (Obsolete; legacy LLM-based flat extraction path replaced by xbrl_tree.py)
* `agent3_modeler.py` (Obsolete; tied to old structured JSON format)
* `financial_utils.py` (Obsolete; hardcoded mappings for old 40-code architecture)
* `xbrl_group.py` (Obsolete; superseded by tree-based modeling)
* `patch_sheet_builder.py` (Development artifact)
* `patch_xbrl_tree.py` (Development artifact)

### Files to ADD
* `sheet_builder.py`: Dedicated presentation layer. Extracts `write_sheets()` from `pymodel.py` to decouple the financial math engine from Google Sheets API and formatting logic.
* `tests/test_model_historical.py`: Unit/integration tests for historical loading + invariant verification (see Section 4).
* `tests/test_model_forecast.py`: Forecast computation + sheet formula tests (see Section 4).
* `tests/fixtures/aapl_structured.json`: Real Apple structured output, committed to repo.
* `tests/fixtures/forecast_spec.json`: Sample driver assumptions for forecast tests.

### Files NOT added (deferred)
* ~~`schemas.py`~~: **Deferred.** Anthropic Tool Use `input_schema` dicts inline in `structure_financials.py` and `agent3_modeler.py` already enforce output shape. Extract a shared `schemas.py` only when 3+ consumers exist or inline schemas become unwieldy.

### Files to CHANGE
* `pymodel.py`: **The core financial engine.** Owns load → compute → verify (but hands off presentation). Changes:
  - Add enforce-by-construction API: `set_category()`, `set_is_cascade()`, `set_bs_totals()`, `set_cf_totals()`, `set_cf_cash()` — tautological invariants become impossible to violate (see Section 3)
  - Refactor `compute_model()` to use the enforce-by-construction API + inline assertions for real checks after each forecast step
  - Reduce `verify_model()` to only the 5 real cross-statement checks (BS balance, cash link, NI/D&A/SBC links)
  - Add `--checkpoint` mode: runs `load_filing()` + `verify_model()` only, writes `historical_baseline.json` (Phase 1 checkpoint)
  - Return a structured `ModelResult` object instead of calling `write_sheets()`. All presentation and Google Sheets logic is extracted to `sheet_builder.py`.
  - Accept optional `--forecast forecast_spec.json` to use LLM-derived drivers instead of hardcoded defaults
* `run_pipeline.py`: Update to act as the pipeline controller orchestrating `xbrl_tree.py`, `pymodel.py`, and `sheet_builder.py`.

### Why `pymodel.py` owns the math (but not presentation)

The spec originally proposed a separate `build_historical_baseline.py`, but `pymodel.py` already handles the heavy lifting of financial logic:

| Responsibility | `pymodel.py` function | Lines |
|---------------|----------------------|-------|
| Raw JSON → flex-row codes + catch-alls | `load_filing()`, `_load_is_fallback`, `_load_bs_fallback`, `_load_cf_fallback`, `_assign_category` | 242–668 |
| Pre-classified data loading | `load_preclassified()` | 267–302 |
| Complete-period filtering (IS+BS+CF) | `load_filing()` | 333–340 |
| Forecast computation (drivers → IS/BS/CF) | `compute_model()` | 686–1009 |
| All invariant checks | `verify_model()` | 1016–1071 |

Creating a second file for historical data would split the source of truth for financial rules. Instead, `pymodel.py` gains a `--checkpoint` flag that runs only load + verify and writes the intermediate JSON. 

However, to prevent `pymodel.py` from becoming a "God Object," the presentation logic (`write_sheets()`, lines 1078-1240) is extracted into a dedicated `sheet_builder.py`. `pymodel.py` outputs a pure data structure (`ModelResult`), keeping the physics engine decoupled from Google Sheets formatting.

---

## 2. Data Pipeline & File Roles

The pipeline is split into two isolated state machines. Phase 1 must succeed and produce a mathematically perfect checkpoint before Phase 2 is allowed to run.

### Phase 1: Establish the "Ground Truth"
**Goal:** Convert raw XBRL data into a mathematically verified, 5-year historical baseline.

**Step 1.1: Fetching**
* **File:** `agent1_fetcher.py` (and Managed Agents: `fetch_10k.py`, `fetch_20f.py`)
* **Input:** User query (`"AAPL"`) and `--years 5`.
* **Output:** JSON array of SEC filing URLs and access to the SEC CompanyFacts API data.

**Step 1.2: Extraction (Deterministic XBRL)**
* **File:** `extract_sections.py`
* **Input:** SEC filing URL / Company Ticker.
* **Process:** Uses SEC CompanyFacts API or inline XBRL parsing to extract mathematically perfect historical data. Python natively fetches a dictionary mapping XBRL tags to values across periods (no HTML scraping or LLM OCR for financials).
* **Output:** `raw_xbrl_facts.json` (A dictionary of all company-specific accounting tags and their numerical values) and the raw MD&A HTML slice.

**Step 1.3: Semantic Structuring (LLM as Classifier)**
* **File:** `structure_financials.py`
* **Input:** A list of extracted XBRL keys/tags (e.g., `["Revenues", "CostOfGoodsAndServicesSold"]`).
* **Process:** The LLM does NO extraction of numbers. Its only job is to read the tag names and classify them into the standard schema categories (Revenue, COGS, OpEx, etc.) via Tool Use with inline `input_schema`.
* **Output:** `xbrl_mapping.json` (A classification map linking SEC XBRL tags to the standard schema).

**Step 1.4: Historical Verification (The Physics Engine)**
* **File:** `pymodel.py --checkpoint`
* **Input:** `raw_xbrl_facts.json` and `xbrl_mapping.json`.
* **Process:** 
  1. `load_filing()`: Python applies the LLM's classification map (`xbrl_mapping.json`) to the raw XBRL numbers (`raw_xbrl_facts.json`) to populate the standard model.
  2. Filters to periods with complete data (IS+BS+CF all present).
  3. Picks top 3 items per category by magnitude, assigns catch-all = subtotal - sum(flex). All assignment, no plugging.
  4. `verify_model()`: Checks all invariants (category sums, BS balance, CF cash proof, IS GP).
  5. *Agentic Loop (max 3 iterations):* If invariants fail, feeds exact errors back to `structure_financials.py` to adjust the classification map. If still failing after 3 retries, abort with the remaining errors — do not loop indefinitely.
* **Output:** A mathematically perfect `historical_baseline.json`.

---

### Phase 2: The Forecast Layer
**Goal:** Generate business drivers from the MD&A, project the future, calculate sanity checks, and output a dynamic Google Sheet.

**Step 2.1: Model Reasoning**
* **File:** `agent3_modeler.py`
* **Input:** `historical_baseline.json` and `MDA.html`.
* **Process:** The LLM reads the management discussion and the perfect historical baseline. It outputs business assumptions (e.g., "iPhone unit sales will grow at 5% because of X factor in the MD&A"). It performs NO math.
* **Output:** `forecast_spec.json` (validated via Tool Use `input_schema`).

**Step 2.2: Forecast Computation & Sheet Building**
* **Files:** `pymodel.py --forecast forecast_spec.json` and `sheet_builder.py`
* **Input:** `historical_baseline.json` (from Step 1.4) and `forecast_spec.json` (from Step 2.1).
* **Process:** 
  1. `compute_model()`: Applies forecast drivers to historical baseline — IS (revenue growth, margin %s), BS (DSO/DIO/DPO, PP&E rollforward, equity rollforward), CF (NI + D&A + SBC + WC changes). Interleaved per period: BS non-cash → CF → BS_CASH = CF ending cash.
  2. `verify_model()`: Re-runs all invariants across both historical and forecast periods.
  3. Sanity checks (implied gross margin, implied headcount). If checks fail thresholds, loops back to `agent3_modeler.py` (max 3 iterations). If still failing after 3 retries, abort with the failing sanity checks — do not loop indefinitely.
  4. `pymodel.py` returns a structured `ModelResult` to the pipeline controller.
  5. `sheet_builder.py` takes the `ModelResult` and writes to Google Sheets with live formulas — SUMIF for historical, driver formulas for forecast, cross-sheet refs (BS_CASH = CF ending cash, CF_NI = IS Net Income). **On failure (e.g., API rate limit partway through), delete the partial Sheet and retry from scratch** — do not attempt partial recovery.
* **Output:** A dynamic Google Sheet containing:
  - `Filing`: Raw data with codes in column A.
  - `IS`, `BS`, `CF`: 3-statement model with live formulas.
  - `Rev Build`, `Expense Build`: Standalone tabs documenting the LLM's business drivers and citations.
  - `Sanity Checks`: Standalone tab displaying implied margins and operational metrics.
  - `Summary`: Master dashboard with invariant check row.

---

### Summary of Contracts (Input / Output Boundaries)

1. **XBRL Tags** → `structure_financials.py` → **`xbrl_mapping.json` (LLM Classification Map)**
2. **`xbrl_mapping.json` + `raw_xbrl_facts.json`** → `pymodel.py --checkpoint` → **`historical_baseline.json` (Mathematically Perfect)**
3. **`historical_baseline.json` + MD&A** → `agent3_modeler.py` → **`forecast_spec.json` (Business Drivers Only)**
4. **`historical_baseline.json` + `forecast_spec.json`** → `pymodel.py --forecast` → **Dynamic Google Sheet URL**

---

## 3. Invariant Programming Architecture

The model enforces correctness at two levels: **tautological invariants** (impossible to violate by API design) and **real checks** (cross-statement links that must be verified). The goal is to make bugs impossible where we can, and catch them immediately where we can't.

### Tautological vs Real Invariants

| # | Invariant | Type | Enforcement |
|---|-----------|------|-------------|
| 1 | flex + catch_all == subtotal | **Tautological** | `set_category()` computes catch_all as remainder |
| 2 | TCA + TNCA == TA | **Tautological** | `set_bs_totals()` computes TA from components |
| 3 | TCL + TNCL == TL | **Tautological** | `set_bs_totals()` computes TL from components |
| 4 | OPCF + INVCF + FINCF + FX == NETCH | **Tautological** | `set_cf_totals()` computes NETCH as sum |
| 5 | BEGC + NETCH == ENDC | **Tautological** | `set_cf_cash()` computes ENDC from BEGC + NETCH |
| 6 | REVT - COGST == GP | **Tautological** | `set_is_cascade()` computes GP as difference |
| 7 | GP - OPEXT == OPINC | **Tautological** | `set_is_cascade()` computes OPINC as difference |
| 8 | EBT - TAX == INC_NET | **Tautological** | `set_is_cascade()` computes INC_NET as difference |
| 9 | **TA == TL + TE** | **Real check** | TE comes from equity rollforward, independent of assets |
| 10 | **CF_ENDC == BS_CASH** | **Real check** | CF and BS are independent computations that must agree |
| 11 | **IS NI == CF NI** | **Real check** | Two statements must agree on net income |
| 12 | **IS D&A == CF D&A** | **Real check** | Two statements must agree on depreciation |
| 13 | **IS SBC == CF SBC** | **Real check** | Two statements must agree on stock-based comp |

**Tautological invariants (1-8)** are enforced by construction — the API computes derived values from their components, so violation is impossible. These do NOT need runtime checks or tests.

**Real checks (9-13)** are cross-statement links where two independent computations must agree. These are verified with inline assertions after each forecast step, and in `verify_model()`.

### Enforce-by-Construction API

`pymodel.py` must expose helper functions that make tautological invariants unbreakable. Categories remain plain dicts (existing format: `{subtotal_code, flex_codes, catch_all_code}`) — no class needed:

```python
def set_category(model, cat, period, subtotal, flex_values: dict):
    """Set subtotal and flex items for a category dict. Catch-all is computed.
    
    The catch-all is ALWAYS subtotal - sum(flex). The invariant cannot be violated.
    """
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
    # NOTE: TA == TL + TE is NOT enforced here — it's a real check.

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

### Inline Assertions in `compute_model()`

After each forecast step, assert the real checks immediately. This localizes failures to the step that caused them, instead of reporting all downstream carnage at the end.

```python
for fp in forecast_periods:
    # --- IS forecast ---
    set_is_cascade(model, fp, rev, cogs, opext, inc_o=0, tax=ebt * tax_rate)
    # Category distributions...
    
    # --- BS forecast (non-cash) ---
    # ... AR, inventory, AP, PP&E, equity rollforward ...
    te = cat_sum(cat_te, fp)
    tca_no_cash = cat_sum_excl(cat_tca, fp, exclude={bs_cash_code})
    
    # --- CF forecast ---
    opcf = ni + da + sbc + wc_changes
    invcf = -capex
    fincf = cat_sum(cat_fincf, fp)
    set_cf_totals(model, fp, opcf, invcf, fincf)
    
    begc = v(bs_cash_code, prev)
    netch = opcf + invcf + fincf
    set_cf_cash(model, fp, begc, netch)
    
    # --- BS cash = CF ending cash ---
    endc = begc + netch
    set_v(model, bs_cash_code, fp, endc)
    tca = tca_no_cash + endc
    set_bs_totals(model, fp, tca, tnca, tcl, tncl, te)
    
    # --- REAL CHECKS (assert immediately) ---
    assert abs(v("BS_TA", fp) - v("BS_TL", fp) - v("BS_TE", fp)) < 0.5, \
        f"BS balance failed at {fp}: TA={v('BS_TA', fp)}, TL={v('BS_TL', fp)}, TE={v('BS_TE', fp)}"
    assert abs(v("CF_ENDC", fp) - v(bs_cash_code, fp)) < 0.5, \
        f"Cash link failed at {fp}: CF_ENDC={v('CF_ENDC', fp)}, BS_CASH={v(bs_cash_code, fp)}"
```

### `verify_model()` Scope

After the refactor, `verify_model()` only checks the **real invariants** (9-13). The tautological ones (1-8) are impossible to violate and do not need verification. This reduces `verify_model()` from 13 checks to 5, and every failure it reports is a genuine modeling bug, not a bookkeeping error.

---

## 4. Testing & Invariant Validation

**Invariant Checks as Test Requirements:**
To guarantee the mathematical soundness of the pipeline, rigorous testing must be implemented around the invariant conditions. 
- Any code changes to the extraction, structuring, or forecasting logic must pass an automated test suite that executes the full invariant check cycle.
- **Strict Zero-Balance Rule:** Tests must assert that all invariants (e.g., `Assets - Liabilities - Equity`, `Beginning Cash + Net Change - Ending Cash`) equal exactly zero. If any balance deviates from zero, the test must fail immediately.
- **Input vs. Formula Verification:** The tests must explicitly verify the structural integrity of the spreadsheet cells. They must assert which numbers are hardcoded inputs (e.g., raw filing data, LLM driver assumptions) and which are strictly formulas (e.g., totals, calculated forecasts, cross-sheet references). No derived value or forecast total should ever be hardcoded.
- This ensures that no feature addition (like adding a new line item category or changing the LLM extraction prompt) can silently break the "physics engine" of the financial model.

### Exact Passing Conditions
A pipeline run or test suite is only considered **PASSING** if it satisfies two strict criteria:

#### 1. The Mathematical Zero-Error Rule
The test must evaluate a matrix of 13 core accounting invariants across **all periods** (both historical baseline years and forecast years). The absolute error of each invariant must be practically zero (within a 0.5 rounding tolerance).

The 13 mandatory invariants are:
1. **Balance Sheet:** `Total Assets - Total Liabilities - Total Equity == 0`
2. **Cash Link:** `CF Ending Cash - BS Cash & Equivalents == 0`
3. **Net Income Link:** `IS Net Income - CF Net Income == 0`
4. **D&A Link:** `IS D&A - CF D&A == 0`
5. **SBC Link:** `IS Stock-Based Comp - CF Stock-Based Comp == 0`
6. **Assets Rollup:** `Current Assets + Non-Current Assets - Total Assets == 0`
7. **Liabilities Rollup:** `Current Liab + Non-Current Liab - Total Liabilities == 0`
8. **Equity Rollup:** `Common Stock + Retained Earnings + Other Equity - Total Equity == 0`
9. **CF Structure:** `Operating CF + Investing CF + Financing CF + FX - Net Change in Cash == 0`
10. **Cash Proof:** `Beginning Cash + Net Change in Cash - Ending Cash == 0`
11. **IS Gross Profit:** `Revenue - COGS - Gross Profit == 0`
12. **IS EBIT:** `Gross Profit - OpEx - Operating Income == 0`
13. **IS Net Income:** `EBT - Tax - Net Income == 0`

*(In the generated Google Sheet, this maps exactly to the `TOTAL ERRORS (must be 0)` row evaluating to `0` for every single column).*

#### 2. The Structural Cell-Type Audit
Even if the math perfectly balances, the test must fail if the spreadsheet is "dead" (hardcoded numbers where formulas should be). The test suite must assert the cell data type:
* **Input Condition (Must be Static Numbers):** All raw historical baseline data and all forecast driver assumptions generated by the LLM.
* **Formula Condition (Must be `=FORMULA()`):** All forecast period line items, all totals and rollups in both historical and forecast periods, and all 13 Invariant Check rows.
If the test detects a static number in a forecast total cell, or a formula in a raw historical data cell, the test fails immediately.

### Test Architecture

Tests are split into two layers matching the pipeline phases. All tests run via `pytest` with no network access required.

#### Layer 1: Data → Model (historical loading + verification)

**File:** `tests/test_model_historical.py`
**Fixture:** `tests/fixtures/aapl_structured.json` — real Apple structured output from `structure_financials.py`, committed to repo.

| Test | What it checks |
|------|---------------|
| `test_load_filing_periods` | `load_filing()` returns only periods with IS+BS+CF data; drops incomplete years |
| `test_load_filing_categories` | Every category has subtotal, flex codes, catch-all code |
| `test_set_category_computes_catchall` | `set_category()` always produces `catch_all = subtotal - sum(flex)` — tautological by construction |
| `test_is_cascade_computes_gp_opinc_ni` | `set_is_cascade()` produces correct GP, OPINC, EBT, INC_NET — tautological by construction |
| `test_bs_balance` | **Real check:** `TA == TL + TE` for all historical periods (equity rollforward vs assets) |
| `test_cash_link` | **Real check:** `CF_ENDC == BS_CASH` for all historical periods |
| `test_ni_link` | **Real check:** IS `INC_NET` == CF net income for all historical periods |
| `test_verify_model_zero_errors` | `verify_model()` returns empty list (only real checks, no tautological noise) |
| `test_preclassified_matches_fallback` | If fixture has `_flex_categories`, loading via preclassified path produces same model as fallback path |

#### Layer 2: Model → Excel (forecast + sheet output)

**File:** `tests/test_model_forecast.py`
**Fixture:** Same `aapl_structured.json`, plus `tests/fixtures/forecast_spec.json` (sample driver assumptions).

| Test | What it checks |
|------|---------------|
| `test_forecast_invariants` | All 13 invariants hold for forecast periods (not just historical) |
| `test_bs_cash_equals_cf_endc` | `BS_CASH == CF_ENDC` for every forecast period (assignment, not plug) |
| `test_equity_rollforward` | `CS_new = CS_prev + SBC + stpay`, `RE_new = RE_prev + NI + buyback + dividend` |
| `test_cf_opcf_components` | `OPCF = NI + DA + SBC + WC changes` |
| `test_revenue_growth` | Forecast revenue grows at expected rate from last historical |
| `test_margin_stability` | Forecast COGS%, OpEx% are held constant from last historical |

**Sheet formula tests** (also in `tests/test_model_forecast.py` — same code path, no separate file needed):

| Test | What it checks |
|------|---------------|
| `test_historical_cells_are_sumif` | All historical period cells reference Filing sheet via SUMIF |
| `test_forecast_cells_are_formulas` | No hardcoded numbers in forecast columns |
| `test_totals_are_sums` | Subtotal rows are SUM formulas, not hardcoded |
| `test_cross_sheet_refs` | BS_CASH references CF!ENDC, CF NI references IS!INC_NET |
| `test_invariant_row_formulas` | Check rows (TA-TL-TE, etc.) are formula-based |

*Note: Sheet formula tests are phased to match the incremental `write_sheets()` rewrite. Phase A tests land first (invariant row formulas), remaining tests land as each phase ships.*

#### Running Tests

```bash
# All tests (no network, no API keys needed)
pytest tests/

# Just historical invariants
pytest tests/test_model_historical.py

# Just forecast + sheet
pytest tests/test_model_forecast.py
```

---

## 5. Execution Phases

To manage complexity, this architecture will be implemented in four distinct, testable phases. Each phase establishes a stable checkpoint before adding the next layer of complexity.

### Phase 1: Establish the "Physics Engine" (Historical Baseline)
**Goal:** Prove the core mathematical engine can load XBRL and verify all invariants, bypassing any UI/Sheet dependencies.

*   **Work Items:**
    *   Delete obsolete files (`agent4_spreadsheet.py`, `create_template.py`, etc.).
    *   Implement tautological API in `pymodel.py` (`set_category`, `set_is_cascade`, etc.).
    *   Refactor `pymodel.py` to support `--checkpoint` flag and write `historical_baseline.json`.
    *   Create `tests/test_model_historical.py` with `test_verify_model_zero_errors`.
    *   Update `structure_financials.py` to use inline Pydantic models for Tool Use.
*   **Deliverables:** A runnable `pymodel.py --checkpoint` that reads `raw_xbrl_facts.json` and outputs a perfect `historical_baseline.json`.
*   **Exit Criteria:**
    *   `pytest tests/test_model_historical.py` passes with zero mathematical errors (all 5 real invariants pass).
    *   `historical_baseline.json` is structurally valid and contains no unmapped flex items.

### Phase 2: Decoupling & Presentation Layer
**Goal:** Extract presentation logic from the engine and establish the new Google Sheets handoff contract.

*   **Work Items:**
    *   Extract `write_sheets()` from `pymodel.py` into a new `sheet_builder.py`.
    *   Define the `ModelResult` dataclass to act as the boundary between the engine and the presentation layer.
    *   Implement Phase A of formula rewriting in `sheet_builder.py` (Invariant check rows become formulas).
    *   Update `run_pipeline.py` to orchestrate `pymodel.py` and `sheet_builder.py`.
*   **Deliverables:** A pipeline that computes the historical baseline and outputs a Google Sheet with formula-based invariant rows.
*   **Exit Criteria:**
    *   `sheet_builder.py` contains zero financial logic (no awareness of GP, Net Income formulas, etc.).
    *   Generated Google Sheet displays correctly with 0 errors in the Invariant check rows.

### Phase 3: Dynamic Sheet Formulas
**Goal:** Fully migrate the Google Sheet from static numbers to a dynamic, fully linked model.

*   **Work Items:**
    *   Implement Phase B (Subtotals/totals as SUM formulas).
    *   Implement Phase C (Forecast line items as driver formulas referencing assumptions).
    *   Implement Phase D (Historical cells as SUMIF references to raw filing data).
    *   Add corresponding tests to verify cell structural integrity.
*   **Deliverables:** A fully dynamic 3-statement model in Google Sheets where users can modify drivers and see flowing changes.
*   **Exit Criteria:**
    *   `pytest` correctly asserts that no forecast numbers or totals are hardcoded (they must be `=FORMULA()`).
    *   Changing a growth rate driver in the generated Google Sheet updates Revenue, Net Income, and Ending Cash correctly.

### Phase 4: Pydantic Guardrails & Forecast Logic
**Goal:** Implement safe, strictly typed business drivers and compute the forward-looking forecast.

*   **Work Items:**
    *   Define strict Pydantic schemas inline (or in `schemas.py`) for `RevenueDriver`, `MarginDriver`, etc., with numerical bounds.
    *   Update `agent3_modeler.py` to enforce these constraints and explicitly forbid arithmetic in the prompt.
    *   Extend `pymodel.py` to accept `--forecast forecast_spec.json`.
    *   Implement forecast computations in `compute_model()`, asserting real checks after each step.
    *   Create `tests/test_model_forecast.py`.
*   **Deliverables:** An integrated pipeline that takes MD&A to strict drivers, computes the forecast, and verifies invariants across all periods.
*   **Exit Criteria:**
    *   `pytest tests/test_model_forecast.py` passes perfectly.
    *   The LLM is proven to fail fast via Tool Use validation if it hallucinates drivers outside Pydantic bounds.