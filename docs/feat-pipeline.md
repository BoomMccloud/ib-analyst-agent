# SEC Pipeline: Tree-Based 3-Statement Model Specification

## 1. Architecture Overview

The pipeline converts SEC 10-K filings into fully-linked Google Sheets with formula-based financial statements. It is **deterministic-first**: XBRL parsing, tree construction, and sheet rendering are pure Python. LLMs are only used for tasks requiring judgment (filing fetcher agent, optional sibling grouping).

### Core Principle: Three-Layer Merge

The sheet rendering is built on a three-layer merge of XBRL linkbases:

1. **Calc layer** (mathematical truth): Parent-child tree with signed weights (+1/-1). Defines `=SUM(children * weight)` formulas. Source: `_cal.xml`.
2. **Presentation layer** (display order): Sibling ordering matching the 10-K layout (Revenue first, Net Income last). Source: `_pre.xml`.
3. **"Other" layer** (gap absorption): For any parent where `SUM(children) != declared_value`, an "Other" row absorbs the residual. This guarantees every formula equals its declared XBRL value by construction.

This means cross-statement invariants (BS Balance, Cash Link, NI Link) hold in the sheet because every parent's formula produces the exact XBRL-declared number.

### Cash Flow: Mixed Duration + Instant Facts

The CF statement uniquely combines two XBRL context types:
- **Duration facts** (flows): OPCF, INVCF, FINCF, FX, Net Change in Cash — these are in the calc tree.
- **Instant facts** (balances): Beginning Cash, Ending Cash — these are NOT in the calc tree.

The pipeline handles this by:
- Deriving the ending cash concept from the CF root concept (strips `PeriodIncreaseDecrease...` suffix to find the balance version)
- Rendering Beginning Cash as a hard value (prior period's ending balance from XBRL instant facts)
- Rendering Ending Cash as a formula: `=Beginning Cash + Net Change`
- Net Change is already a formula from the calc tree: `=OPCF + INVCF + FINCF + FX`

---

## 2. File Roles

### Pipeline Scripts

| File | Role | LLM? |
|------|------|------|
| `agent1_fetcher.py` | Resolves ticker -> CIK, fetches filing URLs from SEC EDGAR | Managed Agent |
| `xbrl_tree.py` | Parses iXBRL facts + calc/pre linkbases -> reconciled trees with roles | No |
| `pymodel.py` | Verifies 5 cross-statement invariants on reconciled trees | No |
| `sheet_builder.py` | Renders trees to Google Sheets with formulas and cross-statement checks | No |
| `run_pipeline.py` | Orchestrates full pipeline: fetch -> extract -> verify -> sheet | No |

### Supporting Utilities

| File | Role |
|------|------|
| `lookup_company.py` | Ticker/name -> CIK resolution |
| `fetch_10k.py` / `fetch_20f.py` | SEC EDGAR submissions API for filing metadata |
| `parse_xbrl_facts.py` | iXBRL tag extraction from filing HTML |
| `sec_utils.py` | HTTP fetching with rate limiting and caching |
| `gws_utils.py` | Google Sheets API wrapper (`gws` CLI) |
| `llm_utils.py` | Anthropic API wrapper |

### Deleted Files (legacy)

All legacy code from the old 40-code / flat-dict architecture has been removed:
`agent4_spreadsheet.py`, `create_template.py`, `template_row_map.json`, `diagnose_model.py`, `build_model_sheet.py`, `legacy_pymodel.py`, `extract_sections.py`, `structure_financials.py`, `agent3_modeler.py`, `financial_utils.py`, `xbrl_group.py`, `patch_sheet_builder.py`, `patch_xbrl_tree.py`.

---

## 3. Data Pipeline

### Stage 1: Fetch Filings

**Script:** `agent1_fetcher.py`
**Input:** Ticker (e.g., `AAPL`) + `--years N`
**Output:** JSON with filing URLs and company metadata

### Stage 2: Build Reconciled Trees

**Script:** `xbrl_tree.py`
**Input:** Filing URL (e.g., `--url https://...nflx-20251231.htm`)
**Output:** JSON with `IS`, `BS`, `BS_LE`, `CF` trees + `complete_periods` + `cf_endc_values`

This stage performs 9 reconciliation steps (`reconcile_trees()`):

| Step | What it does |
|------|-------------|
| A | Tag BS positions (BS_TA, BS_TL, BS_TE, BS_TCA, BS_TCL, BS_CASH) by tree position |
| B | Tag CF positions (CF_NETCH, CF_OPCF, CF_INVCF, CF_FINCF, CF_FX, INC_NET_CF) by concept pattern |
| C | Tag IS Net Income (INC_NET) by value-matching against CF's authoritative NI |
| D | Tag IS Revenue and COGS (IS_REVENUE, IS_COGS) by BFS keyword search |
| E | Override BS_CASH with CF_ENDC values (cross-statement link) |
| F | Filter to complete periods (only periods with data in all 4 statement trees) |
| G | **Three-layer merge**: reorder by presentation, insert "Other" rows for gaps |
| H | Tag D&A and SBC nodes (IS_DA/CF_DA, IS_SBC/CF_SBC) by time-series value matching |

**Key design decisions:**
- **Position over names**: BS structure identified by tree position (root=TA, last L&E child=TE), not concept name matching. Works across all industries.
- **Presentation ordering via BS4**: The `_pre.xml` is parsed with BeautifulSoup to properly resolve locator labels to concept names and flatten the tree hierarchy into a global display order.
- **CF_ENDC derivation**: The ending cash tag is derived from the CF root concept by stripping the `PeriodIncreaseDecrease` suffix. Falls back to common tags. This handles companies like TSLA that use `...IncludingDisposalGroupAndDiscontinuedOperations`.
- **"Other" rows**: Bottom-up insertion. For each branch node, `Other = declared_value - SUM(children * weight)`. Can be positive (missing items) or negative (overshoot). 96% of parent nodes need no Other row.

### Stage 3: Verify Invariants

**Script:** `pymodel.py --trees trees.json --checkpoint`
**Input:** Reconciled trees JSON
**Output:** Pass/fail with error details

Checks 5 cross-statement invariants:

| # | Invariant | What it catches |
|---|-----------|----------------|
| 1 | BS_TA == BS_TL + BS_TE | Balance sheet doesn't balance |
| 2 | CF_ENDC == BS_CASH | Cash flow ending cash != balance sheet cash |
| 3 | CF_BEGC[t] == BS_CASH[t-1] | Beginning cash != prior period ending cash |
| 4 | INC_NET (IS) == INC_NET (CF) | Net income mismatch across statements |
| 5 | Segment Sums | Child segments must sum exactly to parent |
| 6 | IS_DA == CF_DA | D&A mismatch (using role tags) |
| 7 | IS_SBC == CF_SBC | SBC mismatch (using role tags) |

These are **real checks** — they compare independently computed values that must agree. They cannot be enforced by construction because they cross statement boundaries.

### Stage 4: Write Google Sheet

**Script:** `sheet_builder.py --trees trees.json --company "Company Name"`
**Input:** Reconciled trees JSON + company name
**Output:** Google Sheet URL (JSON to stdout)

Creates a 4-tab Google Sheet:

| Tab | Content |
|-----|---------|
| IS | Income Statement with cascade rendering (Revenue first, NI last) |
| BS | Balance Sheet: Assets section + Liabilities & Equity section |
| CF | Cash Flows: calc tree + Beginning Cash (hard) + Net Change (ref) + Ending Cash (formula) |
| Summary | Key metrics + 5 cross-statement check formulas (should all be 0) |

**Formula construction:**
- Leaf nodes: hard values from XBRL facts
- Branch nodes: `=SUM(children * weight)` using `_build_weight_formula()`
- Other rows: hard values (the residual gap)
- Cross-statement checks: formulas referencing cells across tabs via `global_role_map`
- Ending Cash: `=Beginning Cash + Net Change` (formula, not hardcoded)

### Stage 5: Forecasting (Future)

Not yet implemented. Will add LLM-driven forecast drivers applied to the historical baseline.

---

## 4. Invariant Architecture

### What Changed from the Original Spec

The original spec proposed 13 invariants split into "tautological" (enforced by construction in `pymodel.py`) and "real" (cross-statement checks). The implementation evolved:

**Original approach (flat-dict model):** `pymodel.py` owned financial math via `set_category()`, `set_is_cascade()`, `set_bs_totals()`, etc. Tautological invariants were enforced by these APIs.

**Current approach (tree-based model):** The XBRL calc tree IS the mathematical model. Parent-child relationships with signed weights define the formulas. `pymodel.py` doesn't compute anything — it only verifies cross-statement links. The "tautological" invariants are now enforced by the tree structure + "Other" row mechanism:

| Original Invariant | How it's enforced now |
|-------------------|----------------------|
| flex + catch_all == subtotal | Tree: `=SUM(children * weight)` + Other row = declared value |
| TCA + TNCA == TA | Tree: TA node's children include TCA and TNCA |
| OPCF + INVCF + FINCF + FX == NETCH | Tree: CF root's children are OPCF, INVCF, FINCF, FX |
| BEGC + NETCH == ENDC | Sheet formula: Ending Cash `=Beginning Cash + Net Change` |
| Revenue - COGS == GP | Tree: GP node's children are Revenue(+1) and COGS(-1) |

The 7 real cross-statement checks remain in `verify_model()`.

### Declarative Cross-Statement Checks

Cross-statement checks are defined declaratively in `xbrl_tree.py`:

```python
CROSS_STATEMENT_CHECKS = [
    {"name": "BS Balance (TA-TL-TE)", "roles": ["BS_TA", "BS_TL", "BS_TE"],
     "formula": "={BS_TA}-{BS_TL}-{BS_TE}"},
    {"name": "Cash Link (CF_ENDC-BS_CASH)", "roles": ["CF_ENDC", "BS_CASH"],
     "formula": "={left}-{right}"},
    {"name": "NI Link (IS-CF)", "roles": ["INC_NET", "INC_NET_CF"],
     "formula": "={left}-{right}"},
    {"name": "D&A Link (IS-CF)", "roles": ["IS_DA", "CF_DA"],
     "formula": "={left}-{right}"},
    {"name": "SBC Link (IS-CF)", "roles": ["IS_SBC", "CF_SBC"],
     "formula": "={left}-{right}"},
]
```

`sheet_builder.py` renders these into the Summary tab. Checks with missing roles (e.g., D&A for companies without a separate D&A line) are silently skipped.

---

## 5. Testing

### Test Files

| File | What it tests |
|------|-------------|
| `tests/test_dual_linkbase.py` | 28 tests: presentation parsing, cascade layout, IS tagging, orphan supplementation, tree completeness, cross-statement checks, pipeline gate |
| `tests/test_model_historical.py` | `verify_model()` on reconciled trees using synthetic tree fixtures |
| `tests/test_offline_e2e.py` | End-to-end pipeline from cached filings |
| `tests/test_sheet_formulas.py` | `_build_weight_formula()` and `dcol()` helpers |
| `tests/test_da_sbc_tagging.py` | D&A and SBC cross-statement tagging |
| `test_merge_layers.py` | 11 tests: three-layer merge algorithm (9 synthetic + 2 real-world covering all companies) |
| `test_alignment.py` | Validates calc vs presentation linkbase alignment across all cached companies |

### Test Fixtures

10 company fixtures in `tests/fixtures/sec_filings/`: AAPL, AMZN, BRK-B, GOOG, JPM, META, MSFT, NFLX, PFE, TSLA. Each has cached filing HTML, `_cal.xml`, `_pre.xml`, and pre-built `trees.json`.

### Running Tests

```bash
# All unit tests
python -m pytest tests/test_dual_linkbase.py -v

# Three-layer merge tests (synthetic + 10 real companies)
python test_merge_layers.py

# Full pipeline (generates Google Sheet)
python run_pipeline.py AAPL
```

---

## 6. Tested Companies & Known Edge Cases

### Scorecard (as of 2026-04-11)

| Company | Industry | BS Balance | NI Link | Cash Proof | Other Rows | Notes |
|---------|----------|-----------|---------|------------|------------|-------|
| NFLX | Streaming | 0 | 0 | 0 | IS: 0, BS: 0 | Clean pass |
| AAPL | Tech HW | 0 | 0 | 0 | 0 | Clean pass, no Other rows needed |
| XOM | Oil & Gas | 0 | 0 | 0 | 0 | D&A tagged, oil-specific line items |
| JPM | Banking | 0 | 0 | 0 | BS: 1 | Bank-style IS (net interest income) |
| TSLA | Auto/EV | 0 | 0 | 0 | BS: 1 | Cash Link shows restricted cash diff (~$900) |
| GE | Conglomerate | 0 | 104* | no BEGC** | IS: 2 | *Discontinued ops; **only 2 instant dates |
| AMZN | E-commerce | 0 | 0 | 0 | IS: 2, BS: 1 | |
| GOOG | Tech | 0 | 0 | 0 | IS: 2, BS: 2 | |
| META | Social | 0 | 0 | 0 | IS: 2, BS: 1 | |
| PFE | Pharma | -213K*** | — | — | Multiple | ***Pre-existing BS root selection issue |

### Known Edge Cases

1. **MSFT**: Uses XBRL Calculation 1.1 (`calculation-1.1.xsd`) instead of traditional `_cal.xml`. Our regex-based `fetch_cal_linkbase` can't find it. Requires parser update.
2. **GE**: NI Link mismatch because IS reports "Income from Continuing Operations" while CF reports "ProfitLoss" (includes discontinued operations). Real data difference, not a bug.
3. **TSLA**: Cash Link shows ~$900 difference because BS_CASH = `CashAndCashEquivalentsAtCarryingValue` (excludes restricted cash) while CF_ENDC = `...IncludingDisposalGroupAndDiscontinuedOperations` (includes restricted cash).
4. **Dimensional data**: Companies like AAPL report Products/Services revenue split via XBRL dimensions (`ProductOrServiceAxis`), not separate concepts. Our pipeline shows only the total — dimensional breakdowns are a future enhancement.

---

## 7. Execution Phases

### Phase 1: XBRL Tree Engine (COMPLETE)

Deterministic extraction from iXBRL + calculation linkbase. Position-based tagging. 7 cross-statement invariants. Tested on 10 companies.

### Phase 1b: Dual Linkbase + Three-Layer Merge (COMPLETE)

Presentation linkbase parsing (BS4-based), cascade rendering for IS (Revenue first), "Other" rows for calc linkbase gaps, declarative cross-statement checks, tree completeness verification.

### Phase 2: Decoupled Sheet Builder (COMPLETE)

`sheet_builder.py` extracted from `pymodel.py`. Renders trees to Google Sheets with weight-aware formulas. Summary tab with cross-statement check formulas. Cash proof section with formula-derived Ending Cash.

### Phase 3: Dynamic Sheet Formulas (CURRENT)

All subtotals are `=SUM()` formulas. Cross-sheet references via `global_role_map`. Ending Cash = `=Beginning + Net Change`. Check rows are formula-based.

Remaining work:
- SUMIF references to raw filing data (historical cells)
- Forecast driver formulas (depends on Phase 4)

### Phase 4: Forecast Layer (FUTURE)

- LLM reads MD&A + historical baseline -> `forecast_spec.json` (business drivers only, no math)
- `pymodel.py` applies drivers to compute forward periods
- `sheet_builder.py` renders forecast columns with driver formulas
- Inline assertions after each forecast step for immediate failure localization

### Phase 5: XBRL Calculation 1.1 Support (FUTURE)

Support for the new XBRL calculation linkbase format used by MSFT and other companies filing with the updated spec.
.
ization

### Phase 5: XBRL Calculation 1.1 Support (FUTURE)

Support for the new XBRL calculation linkbase format used by MSFT and other companies filing with the updated spec.
.
