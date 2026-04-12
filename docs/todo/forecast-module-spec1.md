# Forecast Module — Stage 5: Revenue Forecasting

## TL;DR

> **Quick Summary**: Add Stage 5 to the SEC pipeline that extracts MD&A text and Notes from filing HTML using a Two-Phase LLM Map-and-Slice strategy, uses an LLM to identify revenue growth drivers per segment, and produces a 5-year revenue forecast as both JSON and a Google Sheet extension.
>
> **Deliverables**:
> - `content_extractor.py` — MD&A and Notes text extraction from filing HTML using ToC mapping and LLM routing
> - `forecast_engine.py` — LLM driver proposal + forecast computation
> - `forecast_sheet.py` — Google Sheet rendering (Forecast + Assumptions tabs)
> - `run_pipeline.py` updated with Stage 5
> - `tests/test_forecast.py` — Unit tests for all components
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES — 3 waves
> **Critical Path**: Content extractor → Driver proposer → Forecast engine → Sheet renderer

---

## Context

### Original Request
Plan a forecasting module that takes existing historical financials and revenue breakdowns, analyzes all revenue segmentations, and based on MD&A text, identifies growth drivers (formulas: a×b like units×ASP, market_share×TAM, or a×(1+r) growth rate). The module must be generic for all companies.

### Interview Summary
**Key Discussions**:
- **Output format**: Both JSON forecast file + Google Sheet extension
- **Forecast horizon**: Fixed 5 years
- **Driver extraction**: LLM-based (reads MD&A, proposes drivers)
- **Scope**: Revenue only (OpEx forecasted separately later)
- **MD&A/Notes sourcing**: Two-Phase Map and Slice strategy. First map ToC links, then have LLM identify the section boundaries, and finally use Python (BeautifulSoup) to slice the HTML without truncation.
- **Scenarios**: Single forecast only (no base/bull/bear)
- **Integration**: Stage 5 in run_pipeline.py

**Research Findings**:
- Revenue segments already exist as `revenue_segments` TreeNode — hierarchical product×segment breakdown
- Text extraction does NOT exist — must be built from scratch using BeautifulSoup
- TreeNode structure: concept, tag, name, weight, values: {period: float}, children, is_leaf, role
- Sheet builder renders 4 tabs with =SUM() formulas, cross-sheet refs, color-coded cells
- Common driver patterns: units×ASP, subscribers×ARPU, market_share×TAM, growth_rate (fallback)

### Metis Review
- Relaxed heading extraction in favor of LLM-routed Map and Slice.
- Clarified Anthropic client initialization.
- Clarified Stage 5 only runs extraction on the latest filing.

---

## Work Objectives

### Core Objective
Build a generic, driver-based revenue forecasting module that works for any company by extracting MD&A narrative and Notes, identifying growth drivers per revenue segment, and projecting 5 years forward.

### Concrete Deliverables
- `content_extractor.py` — Extracts MD&A and Notes text from filing HTML via Two-Phase ToC Map and LLM Slice
- `forecast_engine.py` — LLM proposes drivers, computes 5-year forecast
- `forecast_sheet.py` — Renders Forecast + Assumptions tabs to Google Sheet
- `run_pipeline.py` — Stage 5 integration
- `tests/test_forecast.py` — Unit tests for all components

### Definition of Done
- [ ] `python run_pipeline.py AAPL` produces a Google Sheet with Forecast + Assumptions tabs
- [ ] `python forecast_engine.py --trees trees.json --mda mda.json -o forecast.json` produces valid forecast JSON
- [ ] All tests pass: `python -m pytest tests/test_forecast.py -v`
- [ ] Forecast JSON contains driver assumptions per segment + 5-year projections

### Must Have
- Content extraction from filing HTML using Map and Slice (ToC mapping -> LLM boundary detection -> BeautifulSoup slicing)
- LLM driver proposal per revenue segment (Sonnet)
- Driver formulas powered by a Registry Pattern (`growth_rate`, `market_share`, `units_price`)
- Fallback to historical CAGR when LLM can't identify a driver
- 5-year forecast output as JSON
- Google Sheet extension: Forecast tab + Assumptions tab
- Generic — works for any company/industry

### Must NOT Have (Guardrails)
- No OpEx forecasting (revenue only)
- No multi-scenario support (single forecast)
- No BS/CF forecasting
- No new dependencies beyond existing (anthropic, beautifulsoup4, json)
- No hardcoded company-specific logic
- No modification to existing tree structure or sheet tabs (IS, BS, CF, Summary)

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: YES (pytest)
- **Automated tests**: Tests-after (add test tasks after implementation tasks)
- **Framework**: pytest
- **Agent-Executed QA**: ALWAYS (mandatory for all tasks)

### QA Policy
Every task MUST include agent-executed QA scenarios. Evidence saved to `.sisyphus/evidence/task-{N}-{scenario-slug}.{ext}`.

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately — foundation + extraction):
├── Task 1: Content extractor module [deep]
├── Task 2: Forecast data model + types [quick]
└── Task 3: LLM driver proposer prompt + parser [unspecified-high]

Wave 2 (After Wave 1 — core forecast engine):
├── Task 4: Forecast engine (apply drivers, compute 5-year) [deep]
├── Task 5: Forecast JSON output + validation [quick]
└── Task 6: Unit tests for Tasks 1-5 [unspecified-high]

Wave 3 (After Wave 2 — sheet rendering + pipeline integration):
├── Task 7: Sheet renderer (Forecast + Assumptions tabs) [visual-engineering]
├── Task 8: run_pipeline.py Stage 5 integration [quick]
└── Task 9: End-to-end QA + tests [unspecified-high]

Wave FINAL (After ALL tasks — 4 parallel reviews, then user okay):
├── Task F1: Plan compliance audit (oracle)
├── Task F2: Code quality review (unspecified-high)
├── Task F3: Real manual QA (unspecified-high)
└── Task F4: Scope fidelity check (deep)
```

### Dependency Matrix

- **1**: - → 4
- **2**: - → 4, 5
- **3**: - → 4
- **4**: 1, 2, 3 → 5, 6
- **5**: 2, 4 → 7, 8
- **6**: 4 → 9
- **7**: 5 → 8, 9
- **8**: 5, 7 → 9
- **9**: 6, 7, 8 → F1-F4

### Agent Dispatch Summary

- **Wave 1**: 3 tasks — T1 → `deep`, T2 → `quick`, T3 → `unspecified-high`
- **Wave 2**: 3 tasks — T4 → `deep`, T5 → `quick`, T6 → `unspecified-high`
- **Wave 3**: 3 tasks — T7 → `visual-engineering`, T8 → `quick`, T9 → `unspecified-high`
- **FINAL**: 4 tasks — F1 → `oracle`, F2 → `unspecified-high`, F3 → `unspecified-high`, F4 → `deep`

---

## TODOs

- [ ] 1. **Content Extractor Module (Two-Phase Map and Slice)**

  **What to do**:
  - Create `content_extractor.py` with `extract_sections(html: str) -> dict`
  - Ensure HTML is decoded to string (handling raw bytes from fetch_url if necessary).
  - Phase 1 (Map): Parse filing HTML using BeautifulSoup. Extract all internal anchor links (e.g. `href="#id"`) and their associated text. If missing, collect block-level tags with "Item" or "Note" and an `id`. Create a JSON mapping array of `[{"id": "...", "text": "..."}]`.
  - Phase 2 (Route): Send the JSON map to the LLM (Claude 3.5 Sonnet) using `anthropic.Anthropic()` and `call_llm`. Prompt it to return `start_id` and `end_id` for both the MD&A section and Notes to Financial Statements.
  - Phase 3 (Slice): Use BeautifulSoup to locate `start_id` and iterate over next siblings until `end_id` is reached. Extract the text, strip out `<style>` and `<script>` tags.
  - Return structured output: `{"mda": "...", "notes": "..."}`
  - Handle both 10-K and 20-F filing formats (LLM handles semantic variation).

  **Must NOT do**:
  - No feeding the full HTML to the LLM.
  - No modification to existing xbrl_tree.py or parse_xbrl_facts.py
  - No caching layer (use existing sec_utils cache)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Parallel Group**: Wave 1
  - **Blocks**: Task 4

  **References**:
  - `sec_utils.py`
  - `llm_utils.py`

  **QA Scenarios**:

  ```
  Scenario: Extract MD&A and Notes from a real 10-K filing
    Tool: Bash (python)
    Steps:
      1. python -c "from content_extractor import extract_sections; from sec_utils import fetch_url; html = fetch_url('https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm').decode('utf-8', errors='ignore'); sections = extract_sections(html); print(len(sections['mda']), len(sections['notes']))"
      2. Assert both lengths are > 1000
    Expected Result: MD&A and Notes extracted without truncation.
    Evidence: .sisyphus/evidence/task-1-extract-real-filing.json
  ```

  **Commit**: YES (groups with 2, 3)

---

- [ ] 2. **Forecast Data Model + Types**

  **What to do**:
  - Define data structures for forecast output in `forecast_engine.py` (or separate `forecast_types.py`)
  - Implement a **Registry Pattern** for driver formulas (`DRIVER_FORMULAS` dictionary mapping string names to computation functions).
  - Implement 3 core driver formulas:
    - `growth_rate`: Computes YoY growth (`value[t] = value[t-1] * (1 + rate)`).
    - `market_share`: Computes revenue from TAM and share (`value[t] = TAM[t] * share[t]`).
    - `units_price`: Computes revenue from volume and price (`value[t] = units[t] * price[t]`).
  - `Driver` dataclass: `driver_type: str`, `components: dict[str, float]`, `description: str`, `source: str`. Must validate `driver_type` against the registry.
  - `SegmentForecast` dataclass: `segment_name: str`, `driver: Driver`, `historical_values: dict[str, float]`, `forecast_values: dict[str, float]`. Add a `compute()` method to run the registered formula.
  - `ForecastResult` dataclass: `company: str`, `ticker: str`, `base_year: str`, `forecast_periods: list[str]`, `segments: list[SegmentForecast]`, `total_revenue: dict[str, float]`, `metadata: dict`. Add an `aggregate_totals()` method.
  - Validation: ensure forecast_values has exactly 5 years, segment sums match total for each year
  - JSON serialization/deserialization methods

  **Must NOT do**:
  - No LLM logic here (pure data model)
  - No sheet rendering here

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Parallel Group**: Wave 1
  - **Blocks**: Tasks 4, 5

  **References**:
  - `xbrl_tree.py:404-461`
  - `pymodel.py:1-50`

  **Commit**: YES (groups with 1, 3)

---

- [ ] 3. **LLM Driver Proposer Prompt + Parser**

  **What to do**:
  - Create `propose_drivers(mda_text: str, revenue_segments: dict, historical_values: dict) -> list[Driver]` function
  - Initialize the Anthropic client: `from anthropic import Anthropic; client = Anthropic()`
  - Construct LLM prompt that includes: MD&A text, revenue segment tree structure, historical revenue values per segment
  - Prompt instructs LLM to identify growth driver for each segment and output as structured JSON
  - Driver types constrained to the registry: `growth_rate`, `market_share`, `units_price`
  - Each driver must include: driver_type, components (key-value pairs with numeric values), description, source (MD&A quote)
  - Parse LLM response into `Driver` dataclass instances
  - Handle LLM errors: retry once, fallback to historical CAGR on failure
  - Use `claude-sonnet-4-6` model (precision task)
  - Use existing `llm_utils.py` for API calls (`call_llm(client, model, prompt)`)

  **Must NOT do**:
  - No forecast computation here (only driver proposal)
  - No extraction here (receives text as input)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Parallel Group**: Wave 1
  - **Blocks**: Task 4

  **References**:
  - `llm_utils.py`
  - `agent1_fetcher.py:1-80`

  **Commit**: YES (groups with 1, 2)

---

- [ ] 4. **Forecast Engine — Apply Drivers, Compute 5-Year**

  **What to do**:
  - Create `compute_forecast(drivers: list[Driver], historical: dict, periods: list[str]) -> ForecastResult`
  - Implement driver formulas via the Registry Pattern:
    - `growth_rate`: `value[t] = value[t-1] * (1 + rate)` where rate can vary by year
    - `units_price`: `revenue[t] = units[t] * price[t]` where units and price each have their own growth rates
    - `market_share`: `revenue[t] = TAM[t] * share[t]` where TAM and share each have growth rates
  - Support multi-component drivers (e.g., units growing at 3%, ASP growing at 2%)
  - Compute total revenue as sum of all segment forecasts
  - Validate: segment sums match total for each forecast year
  - Return `ForecastResult` with historical + forecast values

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Parallel Group**: Wave 2
  - **Blocks**: Tasks 5, 6

  **Commit**: YES (groups with 5, 6)

---

- [ ] 5. **Forecast JSON Output + Validation**

  **What to do**:
  - Add CLI interface to `forecast_engine.py`: `python forecast_engine.py --trees <trees.json> --mda <mda.json> -o <forecast.json>`
  - Load trees.json, extract revenue_segments and historical values
  - Load mda.json (output from content_extractor)
  - Call propose_drivers() + compute_forecast()
  - Write forecast.json with full ForecastResult serialization
  - Add `--checkpoint` flag to run validation only

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Parallel Group**: Wave 2
  - **Blocks**: Tasks 7, 8

  **Commit**: YES (groups with 4, 6)

---

- [ ] 6. **Unit Tests for Tasks 1-5**

  **What to do**:
  - Create `tests/test_forecast.py` with comprehensive test coverage
  - Test extractor: ToC mapping, LLM routing (mocked), slicing logic
  - Test data model: serialization, validation, invalid inputs
  - Test driver proposer: mock LLM responses, fallback behavior
  - Test forecast engine: all 4 driver types, multi-segment, segment sum validation
  - Test CLI: flag parsing, file I/O, error handling

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 9

  **Commit**: YES (groups with 4, 5)

---

- [ ] 7. **Sheet Renderer — Forecast + Assumptions Tabs**

  **What to do**:
  - Create `forecast_sheet.py` with `render_forecast_sheet(forecast: ForecastResult, sheet_id: str)`
  - Add "Forecast" tab: revenue by segment for 5 forecast years, total revenue row
  - Add "Assumptions" tab: driver details per segment (driver type, components, description, source)
  - Forecast tab layout: Column C = segment names, Columns E-I = 5 forecast years
  - Assumptions tab layout: Segment | Driver Type | Component | Value | Source
  - Use =SUM() formulas for total revenue row (sum of segment rows)
  - Use `gws` CLI for sheet operations (follow existing sheet_builder.py patterns)
  - Number formatting: currency for revenue, percentage for rates

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Parallel Group**: Wave 3
  - **Blocks**: Tasks 8, 9

  **Commit**: YES (groups with 8)

---

- [ ] 8. **run_pipeline.py Stage 5 Integration**

  **What to do**:
  - Add Stage 5 to `run_pipeline.py` after Stage 4 (sheet builder)
  - Stage 5 flow:
    1. Extract MD&A and Notes sections via `content_extractor.py` using ONLY the latest filing (e.g. `filings[0]['url']`).
    2. Load merged.json for revenue_segments and historical values
    3. Call `propose_drivers()` + `compute_forecast()`
    4. Write forecast.json to pipeline_output/
    5. Call `forecast_sheet.py` to add Forecast + Assumptions tabs to existing sheet
  - Add `--skip-forecast` flag to bypass Stage 5
  - Pass sheet_id from Stage 4 to Stage 5 for tab addition

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Parallel Group**: Wave 3
  - **Blocks**: Task 9

  **Commit**: YES (groups with 7)

---

- [ ] 9. **End-to-End QA + Integration Tests**

  **What to do**:
  - Create `tests/test_forecast_e2e.py` with integration tests
  - Test full flow: Extraction → driver proposal (mocked) → forecast computation → JSON output
  - Test with real trees.json fixtures from pipeline_output/
  - Verify forecast JSON structure matches expected schema
  - Add to CI test suite

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Parallel Group**: Wave 3
  - **Blocks**: Final Verification Wave

  **Commit**: YES

---

## Final Verification Wave (MANDATORY)

- [ ] F1. **Plan Compliance Audit** — `oracle`
- [ ] F2. **Code Quality Review** — `unspecified-high`
- [ ] F3. **Real Manual QA** — `unspecified-high`
- [ ] F4. **Scope Fidelity Check** — `deep`

---

## Success Criteria

### Verification Commands
```bash
python -m pytest tests/test_forecast.py tests/test_forecast_e2e.py -v  # All tests pass
python run_pipeline.py AAPL --skip-forecast  # Stages 1-4 unchanged
python forecast_engine.py --trees trees.json --mda mda.json -o forecast.json  # Valid JSON output
```

### Final Checklist
- [ ] MD&A/Notes extraction works via Map and Slice
- [ ] LLM proposes valid drivers for all revenue segments
- [ ] Fallback to CAGR when LLM fails
- [ ] 5-year forecast computed correctly for all driver types
- [ ] Segment forecasts sum to total revenue
- [ ] forecast.json is valid and complete
- [ ] Google Sheet has Forecast + Assumptions tabs
- [ ] Existing tabs unchanged
- [ ] Stage 5 integrates into run_pipeline.py
- [ ] --skip-forecast flag works
- [ ] All tests pass
