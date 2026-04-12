# Forecast Module — Stage 5: Revenue Forecasting

## TL;DR

> **Quick Summary**: Add Stage 5 to the SEC pipeline that extracts MD&A text from filing HTML, uses an LLM to identify revenue growth drivers per segment, and produces a 5-year revenue forecast as both JSON and a Google Sheet extension.
>
> **Deliverables**:
> - `mda_extractor.py` — MD&A text extraction from filing HTML
> - `forecast_engine.py` — LLM driver proposal + forecast computation
> - `forecast_sheet.py` — Google Sheet rendering (Forecast + Assumptions tabs)
> - `run_pipeline.py` updated with Stage 5
> - `tests/test_forecast.py` — Unit tests for all components
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES — 3 waves
> **Critical Path**: MD&A extractor → Driver proposer → Forecast engine → Sheet renderer

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
- **MD&A sourcing**: Slice from filing HTML using heading-based detection
- **Scenarios**: Single forecast only (no base/bull/bear)
- **Integration**: Stage 5 in run_pipeline.py

**Research Findings**:
- Revenue segments already exist as `revenue_segments` TreeNode — hierarchical product×segment breakdown
- MD&A text extraction does NOT exist — must be built from scratch
- TreeNode structure: concept, tag, name, weight, values: {period: float}, children, is_leaf, role
- Sheet builder renders 4 tabs with =SUM() formulas, cross-sheet refs, color-coded cells
- Common driver patterns: units×ASP, subscribers×ARPU, market_share×TAM, growth_rate (fallback)

### Metis Review
Timed out. Self-review applied gap analysis.

---

## Work Objectives

### Core Objective
Build a generic, driver-based revenue forecasting module that works for any company by extracting MD&A narrative, identifying growth drivers per revenue segment, and projecting 5 years forward.

### Concrete Deliverables
- `mda_extractor.py` — Extracts MD&A text from filing HTML via heading detection
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
- MD&A extraction from filing HTML (heading-based, deterministic)
- LLM driver proposal per revenue segment (Sonnet)
- Driver formulas: units×ASP, market_share×TAM, subscribers×ARPU, growth_rate
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
├── Task 1: MD&A extractor module [deep]
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

- [ ] 1. **MD&A Extractor Module**

  **What to do**:
  - Create `mda_extractor.py` with `extract_mda_sections(html: str) -> list[dict]`
  - Parse filing HTML using BeautifulSoup, detect MD&A section headings (h1-h4 tags containing "Management's Discussion", "MD&A", "Results of Operations", "Liquidity")
  - Extract text content between MD&A heading and next major section heading
  - Return structured output: `{"sections": [{"heading": str, "text": str, "filing_url": str}]}`
  - Handle both 10-K and 20-F filing formats (20-F uses "Operating and Financial Review")
  - Integrate with existing `sec_utils.py` for HTTP fetching

  **Must NOT do**:
  - No LLM-based extraction (deterministic heading detection only)
  - No modification to existing xbrl_tree.py or parse_xbrl_facts.py
  - No caching layer (use existing sec_utils cache)

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Requires understanding of SEC filing HTML structure, BeautifulSoup parsing, edge cases across filing types
  - **Skills**: []
  - **Skills Evaluated but Omitted**:
    - `frontend-design`: Not a UI task

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Tasks 2, 3)
  - **Parallel Group**: Wave 1 (with Tasks 2, 3)
  - **Blocks**: Task 4
  - **Blocked By**: None

  **References**:
  - `sec_utils.py` — SEC-compliant HTTP fetching with rate limiting and caching
  - `xbrl_tree.py:1-50` — Import patterns, TreeNode class structure
  - `parse_xbrl_facts.py:1-60` — BeautifulSoup HTML parsing patterns for XBRL
  - `run_pipeline.py:1-60` — How stages receive filing URLs and pass data

  **Acceptance Criteria**:
  - [ ] `extract_mda_sections()` returns list of dicts with heading, text, filing_url
  - [ ] Handles 10-K and 20-F heading variations
  - [ ] Returns empty list gracefully when no MD&A found (no crash)
  - [ ] Text is clean (no HTML tags, no script/style content)

  **QA Scenarios**:

  ```
  Scenario: Extract MD&A from a real 10-K filing
    Tool: Bash (python)
    Preconditions: AAPL or MSFT filing HTML available (use existing test fixtures or download via sec_utils)
    Steps:
      1. python -c "from mda_extractor import extract_mda_sections; from sec_utils import fetch_url; html = fetch_url('https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm'); sections = extract_mda_sections(html); print(len(sections), [s['heading'] for s in sections])"
      2. Assert len(sections) >= 1
      3. Assert 'Management' in sections[0]['heading'] or 'Discussion' in sections[0]['heading']
      4. Assert '<' not in sections[0]['text']  # No HTML tags
    Expected Result: At least 1 MD&A section extracted with clean text
    Evidence: .sisyphus/evidence/task-1-extract-real-filing.json

  Scenario: Handle filing with no MD&A section
    Tool: Bash (python)
    Preconditions: Create minimal HTML with no MD&A headings
    Steps:
      1. python -c "from mda_extractor import extract_mda_sections; result = extract_mda_sections('<html><body><h1>Financial Statements</h1></body></html>'); print(result)"
      2. Assert result == {'sections': []}
    Expected Result: Returns empty sections list, no exception
    Evidence: .sisyphus/evidence/task-1-no-mda.json
  ```

  **Commit**: YES (groups with 2, 3)
  - Message: `feat(forecast): add MD&A extractor module`
  - Files: `mda_extractor.py`
  - Pre-commit: `python -m pytest tests/test_forecast.py::test_mda_extractor -v`

---

- [ ] 2. **Forecast Data Model + Types**

  **What to do**:
  - Define data structures for forecast output in `forecast_engine.py` (or separate `forecast_types.py`)
  - `Driver` dataclass: `driver_type: str` (units_asp, market_share, growth_rate, subscribers_arpu), `components: dict[str, float]`, `description: str`, `source: str` (MD&A quote or "historical_cagr")
  - `SegmentForecast` dataclass: `segment_name: str`, `driver: Driver`, `historical_values: dict[str, float]`, `forecast_values: dict[str, float]`, `forecast_periods: list[str]`
  - `ForecastResult` dataclass: `company: str`, `ticker: str`, `base_year: str`, `forecast_periods: list[str]`, `segments: list[SegmentForecast]`, `total_revenue: dict[str, float]`, `metadata: dict`
  - Validation: ensure forecast_values has exactly 5 years, segment sums match total for each year
  - JSON serialization/deserialization methods

  **Must NOT do**:
  - No LLM logic here (pure data model)
  - No sheet rendering here
  - No modification to TreeNode class

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Straightforward dataclass definitions with validation, no complex logic
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Tasks 1, 3)
  - **Parallel Group**: Wave 1 (with Tasks 1, 3)
  - **Blocks**: Tasks 4, 5
  - **Blocked By**: None

  **References**:
  - `xbrl_tree.py:404-461` — TreeNode class as pattern for dataclass design
  - `pymodel.py:1-50` — Existing data model patterns
  - `spec_sheet.json:1-50` — JSON structure patterns for sheet specs

  **Acceptance Criteria**:
  - [ ] All dataclasses defined with type hints
  - [ ] `to_dict()` / `from_dict()` methods for JSON round-trip
  - [ ] Validation raises ValueError on invalid driver_type or wrong forecast year count
  - [ ] `segment_sums_match_total()` method verifies segment forecasts sum to total

  **QA Scenarios**:

  ```
  Scenario: Create and serialize a valid forecast
    Tool: Bash (python)
    Preconditions: forecast_types module exists
    Steps:
      1. python -c "
  from forecast_engine import Driver, SegmentForecast, ForecastResult
  d = Driver(driver_type='growth_rate', components={'rate': 0.05}, description='5% growth', source='historical_cagr')
  sf = SegmentForecast(segment_name='Products', driver=d, historical_values={'2024': 300.0}, forecast_values={'2025': 315.0, '2026': 330.75, '2027': 347.29, '2028': 364.65, '2029': 382.88}, forecast_periods=['2025','2026','2027','2028','2029'])
  print(sf.to_dict())
  "
      2. Assert output is valid JSON with all expected keys
      3. Assert len(forecast_values) == 5
    Expected Result: Valid JSON serialization with 5 forecast years
    Evidence: .sisyphus/evidence/task-2-serialize.json

  Scenario: Validation rejects invalid driver type
    Tool: Bash (python)
    Steps:
      1. python -c "from forecast_engine import Driver; Driver(driver_type='invalid', components={}, description='', source='')"
      2. Assert raises ValueError or similar
    Expected Result: Exception raised for invalid driver_type
    Evidence: .sisyphus/evidence/task-2-validation-error.txt
  ```

  **Commit**: YES (groups with 1, 3)
  - Message: `feat(forecast): add forecast data model and types`
  - Files: `forecast_engine.py` (data model portion)
  - Pre-commit: `python -c "from forecast_engine import Driver, SegmentForecast, ForecastResult; print('OK')"`

---

- [ ] 3. **LLM Driver Proposer Prompt + Parser**

  **What to do**:
  - Create `propose_drivers(mda_text: str, revenue_segments: dict, historical_values: dict) -> list[Driver]` function
  - Construct LLM prompt that includes: MD&A text, revenue segment tree structure, historical revenue values per segment
  - Prompt instructs LLM to identify growth driver for each segment and output as structured JSON
  - Driver types constrained to: `units_asp`, `market_share`, `growth_rate`, `subscribers_arpu`
  - Each driver must include: driver_type, components (key-value pairs with numeric values), description, source (MD&A quote)
  - Parse LLM response into `Driver` dataclass instances
  - Handle LLM errors: retry once, fallback to historical CAGR on failure
  - Use `claude-sonnet-4-6` model (precision task)
  - Use existing `llm_utils.py` for API calls

  **Must NOT do**:
  - No forecast computation here (only driver proposal)
  - No MD&A extraction here (receives text as input)
  - No hardcoded company-specific prompts

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Requires careful prompt engineering, JSON schema design, error handling, and integration with existing LLM utilities
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Tasks 1, 2)
  - **Parallel Group**: Wave 1 (with Tasks 1, 2)
  - **Blocks**: Task 4
  - **Blocked By**: None

  **References**:
  - `llm_utils.py` — Existing LLM call patterns, model selection
  - `agent1_fetcher.py:1-80` — Managed Agent usage patterns
  - `xbrl_tree.py:435-448` — TreeNode.to_dict() output format for segment data
  - `docs/xbrl_linkbases.md` — Revenue segment structure documentation

  **Acceptance Criteria**:
  - [ ] `propose_drivers()` returns list of Driver instances
  - [ ] Each driver has valid driver_type from allowed set
  - [ ] Driver components are numeric and sufficient to compute forecast
  - [ ] LLM error handling: retry once, fallback to CAGR
  - [ ] Prompt includes MD&A text + segment structure + historical values

  **QA Scenarios**:

  ```
  Scenario: LLM proposes valid drivers for a real company
    Tool: Bash (python)
    Preconditions: ANTHROPIC_API_KEY set, MD&A text and segment data available
    Steps:
      1. python -c "
  from forecast_engine import propose_drivers
  mda = open('test_mda.txt').read()
  segments = {...}  # Use real segment data from AAPL trees.json
  historical = {...}  # Use real historical values
  drivers = propose_drivers(mda, segments, historical)
  for d in drivers: print(d.driver_type, d.components, d.source)
  "
      2. Assert len(drivers) >= 1
      3. Assert all d.driver_type in ['units_asp', 'market_share', 'growth_rate', 'subscribers_arpu']
      4. Assert all d.components values are numeric
    Expected Result: Valid drivers proposed for each segment
    Evidence: .sisyphus/evidence/task-3-llm-drivers.json

  Scenario: Fallback to CAGR when LLM fails
    Tool: Bash (python)
    Preconditions: Mock LLM to raise exception
    Steps:
      1. python -c "
  from unittest.mock import patch
  from forecast_engine import propose_drivers
  with patch('forecast_engine.call_llm', side_effect=Exception('API error')):
      drivers = propose_drivers('some text', {'Products': {}}, {'Products': {'2023': 100, '2024': 110}})
      print(drivers[0].driver_type, drivers[0].source)
  "
      2. Assert drivers[0].driver_type == 'growth_rate'
      3. Assert drivers[0].source == 'historical_cagr'
      4. Assert drivers[0].components['rate'] == 0.1  # (110-100)/100
    Expected Result: Fallback driver with historical CAGR
    Evidence: .sisyphus/evidence/task-3-fallback.json
  ```

  **Commit**: YES (groups with 1, 2)
  - Message: `feat(forecast): add LLM driver proposer with CAGR fallback`
  - Files: `forecast_engine.py` (driver proposer portion)
  - Pre-commit: `python -c "from forecast_engine import propose_drivers; print('OK')"`

---

- [ ] 4. **Forecast Engine — Apply Drivers, Compute 5-Year**

  **What to do**:
  - Create `compute_forecast(drivers: list[Driver], historical: dict, periods: list[str]) -> ForecastResult`
  - Implement driver formulas:
    - `growth_rate`: `value[t] = value[t-1] * (1 + rate)` where rate can vary by year
    - `units_asp`: `revenue[t] = units[t] * asp[t]` where units and ASP each have their own growth rates
    - `market_share`: `revenue[t] = TAM[t] * share[t]` where TAM and share each have growth rates
    - `subscribers_arpu`: `revenue[t] = subscribers[t] * arpu[t]`
  - Support multi-component drivers (e.g., units growing at 3%, ASP growing at 2%)
  - Compute total revenue as sum of all segment forecasts
  - Validate: segment sums match total for each forecast year
  - Return `ForecastResult` with historical + forecast values

  **Must NOT do**:
  - No LLM calls here (drivers are pre-computed)
  - No sheet rendering here
  - No OpEx or BS/CF forecasting

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Core computation logic with multiple driver types, validation, and edge case handling
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 1, 2, 3)
  - **Parallel Group**: Wave 2 (with Tasks 5, 6)
  - **Blocks**: Tasks 5, 6
  - **Blocked By**: Tasks 1, 2, 3

  **References**:
  - `forecast_engine.py` (Tasks 2, 3 output) — Driver, SegmentForecast, ForecastResult dataclasses
  - `xbrl_tree.py:435-448` — TreeNode.to_dict() for reading historical segment values
  - `pymodel.py:1-50` — Verification patterns (invariant checking)
  - `merge_trees.py:1-50` — Multi-period value handling patterns

  **Acceptance Criteria**:
  - [ ] `compute_forecast()` returns ForecastResult with 5 forecast years
  - [ ] All 4 driver types compute correctly
  - [ ] Segment forecasts sum to total revenue for each year
  - [ ] Historical values are preserved in output
  - [ ] Validation fails if segment sums don't match total

  **QA Scenarios**:

  ```
  Scenario: Growth rate driver computes correct 5-year forecast
    Tool: Bash (python)
    Preconditions: Forecast engine module exists
    Steps:
      1. python -c "
  from forecast_engine import Driver, compute_forecast
  driver = Driver(driver_type='growth_rate', components={'rate': 0.10}, description='10% growth', source='test')
  result = compute_forecast([driver], {'Segment': {'2024': 100.0}}, ['2025','2026','2027','2028','2029'])
  for year, val in result.segments[0].forecast_values.items(): print(year, val)
  "
      2. Assert forecast_values['2025'] == 110.0
      3. Assert forecast_values['2026'] == 121.0
      4. Assert forecast_values['2029'] == 161.051  # 100 * 1.1^5
    Expected Result: Correct compound growth calculation
    Evidence: .sisyphus/evidence/task-4-growth-rate.json

  Scenario: Units×ASP driver computes correct revenue
    Tool: Bash (python)
    Steps:
      1. python -c "
  from forecast_engine import Driver, compute_forecast
  driver = Driver(driver_type='units_asp', components={'units_base': 1000, 'units_growth': 0.05, 'asp_base': 50.0, 'asp_growth': 0.02}, description='Units and ASP growth', source='test')
  result = compute_forecast([driver], {'Products': {'2024': 50000.0}}, ['2025','2026','2027','2028','2029'])
  print(result.segments[0].forecast_values['2025'])
  "
      2. Assert forecast_values['2025'] == 1000*1.05 * 50.0*1.02  # = 53550.0
    Expected Result: Revenue = units × ASP with independent growth rates
    Evidence: .sisyphus/evidence/task-4-units-asp.json

  Scenario: Segment sums must match total
    Tool: Bash (python)
    Steps:
      1. python -c "
  from forecast_engine import compute_forecast, Driver
  d1 = Driver(driver_type='growth_rate', components={'rate': 0.10}, description='', source='')
  d2 = Driver(driver_type='growth_rate', components={'rate': 0.05}, description='', source='')
  result = compute_forecast([d1, d2], {'Products': {'2024': 100.0}, 'Services': {'2024': 50.0}}, ['2025','2026','2027','2028','2029'])
  for year in result.forecast_periods:
      seg_sum = sum(s.forecast_values[year] for s in result.segments)
      total = result.total_revenue[year]
      assert abs(seg_sum - total) < 0.01, f'{year}: {seg_sum} != {total}'
  print('All segment sums match total')
  "
      2. Assert no assertion error
    Expected Result: Segment forecasts sum to total revenue for each year
    Evidence: .sisyphus/evidence/task-4-segment-sums.json
  ```

  **Commit**: YES (groups with 5, 6)
  - Message: `feat(forecast): add forecast computation engine with driver formulas`
  - Files: `forecast_engine.py` (computation portion)
  - Pre-commit: `python -m pytest tests/test_forecast.py::test_compute_forecast -v`

---

- [ ] 5. **Forecast JSON Output + Validation**

  **What to do**:
  - Add CLI interface to `forecast_engine.py`: `python forecast_engine.py --trees <trees.json> --mda <mda.json> -o <forecast.json>`
  - Load trees.json, extract revenue_segments and historical values
  - Load mda.json (output from mda_extractor)
  - Call propose_drivers() + compute_forecast()
  - Write forecast.json with full ForecastResult serialization
  - Add `--checkpoint` flag to run validation only (like pymodel.py --checkpoint)
  - Validation: check driver completeness (all segments have drivers), forecast year count, segment sums

  **Must NOT do**:
  - No sheet rendering here
  - No modification to existing pipeline scripts

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: CLI wrapper around existing functions, straightforward JSON I/O
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 2, 4)
  - **Parallel Group**: Wave 2 (with Tasks 4, 6)
  - **Blocks**: Tasks 7, 8
  - **Blocked By**: Tasks 2, 4

  **References**:
  - `run_pipeline.py:100-160` — CLI argument patterns, JSON file I/O
  - `pymodel.py:1-30` — --checkpoint flag pattern
  - `xbrl_tree.py:1700-1775` — JSON output patterns

  **Acceptance Criteria**:
  - [ ] CLI accepts --trees, --mda, -o flags
  - [ ] Output JSON is valid and contains all ForecastResult fields
  - [ ] --checkpoint runs validation without writing output
  - [ ] Error messages are clear when input files are missing or invalid

  **QA Scenarios**:

  ```
  Scenario: CLI produces valid forecast JSON from test data
    Tool: Bash (python)
    Preconditions: Test trees.json and mda.json files exist
    Steps:
      1. python forecast_engine.py --trees test_trees.json --mda test_mda.json -o test_forecast.json
      2. python -c "import json; d = json.load(open('test_forecast.json')); assert 'segments' in d; assert 'total_revenue' in d; assert len(d['forecast_periods']) == 5; print('Valid')"
    Expected Result: Valid forecast JSON with 5 forecast periods
    Evidence: .sisyphus/evidence/task-5-cli-output.json

  Scenario: --checkpoint validates without writing
    Tool: Bash (python)
    Steps:
      1. python forecast_engine.py --trees test_trees.json --mda test_mda.json --checkpoint
      2. Assert exit code 0
      3. Assert no output file created
    Expected Result: Validation passes, no file written
    Evidence: .sisyphus/evidence/task-5-checkpoint.txt
  ```

  **Commit**: YES (groups with 4, 6)
  - Message: `feat(forecast): add CLI interface and JSON output`
  - Files: `forecast_engine.py` (CLI portion)
  - Pre-commit: `python forecast_engine.py --help`

---

- [ ] 6. **Unit Tests for Tasks 1-5**

  **What to do**:
  - Create `tests/test_forecast.py` with comprehensive test coverage
  - Test MD&A extractor: heading detection, 10-K vs 20-F, empty input, malformed HTML
  - Test data model: serialization, validation, invalid inputs
  - Test driver proposer: mock LLM responses, fallback behavior
  - Test forecast engine: all 4 driver types, multi-segment, segment sum validation
  - Test CLI: flag parsing, file I/O, error handling
  - Use pytest fixtures for common test data (sample trees, sample MD&A)

  **Must NOT do**:
  - No real LLM calls in tests (use mocks)
  - No dependency on external files (use fixtures or inline data)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Requires thorough test coverage across multiple components, mock setup, fixture design
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Task 4)
  - **Parallel Group**: Wave 2 (with Tasks 4, 5)
  - **Blocks**: Task 9
  - **Blocked By**: Task 4

  **References**:
  - `tests/test_dual_linkbase.py` — Existing test patterns, fixture usage
  - `tests/test_sheet_formulas.py` — Formula validation test patterns
  - `tests/test_merge_pipeline.py` — Multi-component test patterns

  **Acceptance Criteria**:
  - [ ] All tests pass: `python -m pytest tests/test_forecast.py -v`
  - [ ] Minimum 15 test cases covering all components
  - [ ] No real LLM calls (all mocked)
  - [ ] Test fixtures are reusable and well-documented

  **QA Scenarios**:

  ```
  Scenario: All unit tests pass
    Tool: Bash (python)
    Steps:
      1. python -m pytest tests/test_forecast.py -v --tb=short
      2. Assert exit code 0
      3. Assert 0 failures, 0 errors
      4. Capture output to .sisyphus/evidence/task-6-test-output.txt
    Expected Result: All tests pass with 0 failures
    Evidence: .sisyphus/evidence/task-6-test-output.txt
  ```

  **Commit**: YES (groups with 4, 5)
  - Message: `test(forecast): add unit tests for all forecast components`
  - Files: `tests/test_forecast.py`
  - Pre-commit: `python -m pytest tests/test_forecast.py -v`

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
  - Do NOT modify existing IS, BS, CF, Summary tabs

  **Must NOT do**:
  - No modification to existing sheet tabs
  - No new dependencies beyond gws CLI
  - No OpEx or BS/CF forecast rendering

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: Sheet rendering requires careful layout, formatting, and formula construction
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Task 5)
  - **Parallel Group**: Wave 3 (with Tasks 8, 9)
  - **Blocks**: Tasks 8, 9
  - **Blocked By**: Task 5

  **References**:
  - `sheet_builder.py:1-100` — gws CLI usage patterns, sheet creation
  - `sheet_builder.py:445-499` — _render_revenue_segments() as pattern for tab rendering
  - `sheet_builder.py:902-908` — CROSS_STATEMENT_CHECKS formula patterns
  - `gws_utils.py` — gws helper functions
  - `spec_sheet.json` — Sheet formatting spec patterns

  **Acceptance Criteria**:
  - [ ] Forecast tab created with segment rows and 5 year columns
  - [ ] Assumptions tab created with driver details
  - [ ] Total revenue row uses =SUM() formula
  - [ ] Currency formatting for revenue values
  - [ ] Existing tabs (IS, BS, CF, Summary) are unchanged

  **QA Scenarios**:

  ```
  Scenario: Forecast tab renders correctly
    Tool: Bash (python)
    Preconditions: gws CLI authenticated, forecast.json exists
    Steps:
      1. python forecast_sheet.py --forecast test_forecast.json --sheet-id <test_sheet_id>
      2. Use gws CLI to read Forecast tab: gws sheets values get <sheet_id> --range "Forecast!C1:I20"
      3. Assert Column C contains segment names
      4. Assert Column E contains first forecast year values
      5. Assert total revenue row contains =SUM formula
    Expected Result: Forecast tab with correct layout and formulas
    Evidence: .sisyphus/evidence/task-7-forecast-tab.json

  Scenario: Assumptions tab shows driver details
    Tool: Bash (python)
    Steps:
      1. Use gws CLI to read Assumptions tab: gws sheets values get <sheet_id> --range "Assumptions!A1:E20"
      2. Assert header row: Segment | Driver Type | Component | Value | Source
      3. Assert at least one driver row with valid data
    Expected Result: Assumptions tab with driver details
    Evidence: .sisyphus/evidence/task-7-assumptions-tab.json
  ```

  **Commit**: YES (groups with 8)
  - Message: `feat(forecast): add sheet renderer for Forecast and Assumptions tabs`
  - Files: `forecast_sheet.py`
  - Pre-commit: `python forecast_sheet.py --help`

---

- [ ] 8. **run_pipeline.py Stage 5 Integration**

  **What to do**:
  - Add Stage 5 to `run_pipeline.py` after Stage 4 (sheet builder)
  - Stage 5 flow:
    1. Download filing HTML (reuse existing filing URL from Stage 1)
    2. Extract MD&A sections via `mda_extractor.py`
    3. Load trees.json for revenue_segments and historical values
    4. Call `propose_drivers()` + `compute_forecast()`
    5. Write forecast.json to pipeline_output/
    6. Call `forecast_sheet.py` to add Forecast + Assumptions tabs to existing sheet
  - Add `--skip-forecast` flag to bypass Stage 5
  - Update Stage 5 placeholder comment ("Coming Soon") to actual implementation
  - Pass sheet_id from Stage 4 to Stage 5 for tab addition

  **Must NOT do**:
  - No modification to Stages 1-4 logic
  - No changes to existing output file paths
  - No new CLI flags beyond --skip-forecast

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Integration task — wiring existing modules into pipeline, minimal new logic
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 5, 7)
  - **Parallel Group**: Wave 3 (with Tasks 7, 9)
  - **Blocks**: Task 9
  - **Blocked By**: Tasks 5, 7

  **References**:
  - `run_pipeline.py` — Full file: pipeline orchestration, stage sequencing, CLI args
  - `sheet_builder.py:1-50` — How sheet_id is passed and used
  - `gws_utils.py` — Sheet ID handling patterns

  **Acceptance Criteria**:
  - [ ] `python run_pipeline.py AAPL` runs all 5 stages including forecast
  - [ ] `python run_pipeline.py AAPL --skip-forecast` runs stages 1-4 only
  - [ ] forecast.json written to pipeline_output/
  - [ ] Google Sheet has Forecast + Assumptions tabs after pipeline completes
  - [ ] Stages 1-4 output unchanged

  **QA Scenarios**:

  ```
  Scenario: Full pipeline runs with Stage 5
    Tool: Bash (python)
    Preconditions: ANTHROPIC_API_KEY set, gws authenticated
    Steps:
      1. python run_pipeline.py AAPL 2>&1 | tee .sisyphus/evidence/task-8-pipeline-output.txt
      2. Assert exit code 0
      3. Assert "Stage 5" or "Forecast" appears in output
      4. Assert pipeline_output/forecast.json exists
      5. Use gws to verify Forecast tab exists in sheet
    Expected Result: Pipeline completes with forecast output and sheet tabs
    Evidence: .sisyphus/evidence/task-8-pipeline-output.txt

  Scenario: --skip-forecast bypasses Stage 5
    Tool: Bash (python)
    Steps:
      1. python run_pipeline.py AAPL --skip-forecast 2>&1 | tee .sisyphus/evidence/task-8-skip-output.txt
      2. Assert exit code 0
      3. Assert "Stage 5" does NOT appear in output
      4. Assert pipeline_output/forecast.json does NOT exist
    Expected Result: Pipeline completes without forecast
    Evidence: .sisyphus/evidence/task-8-skip-output.txt
  ```

  **Commit**: YES (groups with 7)
  - Message: `feat(forecast): integrate Stage 5 into run_pipeline.py`
  - Files: `run_pipeline.py`
  - Pre-commit: `python run_pipeline.py --help`

---

- [ ] 9. **End-to-End QA + Integration Tests**

  **What to do**:
  - Create `tests/test_forecast_e2e.py` with integration tests
  - Test full flow: MD&A extraction → driver proposal (mocked) → forecast computation → JSON output
  - Test with real trees.json fixtures from pipeline_output/
  - Test with synthetic MD&A text for deterministic testing
  - Verify forecast JSON structure matches expected schema
  - Test edge cases: company with no revenue segments, company with single segment, company with many segments
  - Add to CI test suite

  **Must NOT do**:
  - No real LLM calls in e2e tests
  - No real Google Sheet operations in tests

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Integration testing across multiple components, fixture management, edge case coverage
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 6, 7, 8)
  - **Parallel Group**: Wave 3 (with Tasks 7, 8)
  - **Blocks**: Final Verification Wave
  - **Blocked By**: Tasks 6, 7, 8

  **References**:
  - `tests/test_offline_e2e.py` — Existing e2e test patterns
  - `test_merge_layers.py` — Real company test patterns
  - `pipeline_output/` — Existing trees.json fixtures

  **Acceptance Criteria**:
  - [ ] All e2e tests pass: `python -m pytest tests/test_forecast_e2e.py -v`
  - [ ] Tests run without ANTHROPIC_API_KEY (all LLM calls mocked)
  - [ ] Tests run without gws authentication
  - [ ] Edge cases covered: no segments, single segment, many segments

  **QA Scenarios**:

  ```
  Scenario: Full flow with real trees and synthetic MD&A
    Tool: Bash (python)
    Preconditions: pipeline_output/ contains trees.json for at least one company
    Steps:
      1. python -c "
  import json
  from forecast_engine import propose_drivers, compute_forecast
  trees = json.load(open('pipeline_output/aapl_trees.json'))
  mda_text = 'Revenue grew 5% driven by strong iPhone sales and services growth.'
  segments = trees.get('revenue_segments', {})
  historical = {node['name']: node.get('values', {}) for node in [segments]} if segments else {}
  # Mock LLM, test compute
  from unittest.mock import patch
  with patch('forecast_engine.call_llm') as mock_llm:
      mock_llm.return_value = json.dumps({'drivers': [{'segment': 'Products', 'driver_type': 'growth_rate', 'components': {'rate': 0.05}, 'description': '5% growth', 'source': 'MD&A'}]})
      drivers = propose_drivers(mda_text, segments, historical)
      result = compute_forecast(drivers, historical, ['2025','2026','2027','2028','2029'])
      assert len(result.forecast_periods) == 5
      print('E2E test passed')
  "
      2. Assert exit code 0
    Expected Result: Full flow produces valid 5-year forecast
    Evidence: .sisyphus/evidence/task-9-e2e-flow.txt

  Scenario: All forecast tests pass
    Tool: Bash (python)
    Steps:
      1. python -m pytest tests/test_forecast.py tests/test_forecast_e2e.py -v --tb=short
      2. Assert 0 failures, 0 errors
      3. Assert test count >= 20
    Expected Result: All forecast tests pass
    Evidence: .sisyphus/evidence/task-9-all-tests.txt
  ```

  **Commit**: YES
  - Message: `test(forecast): add end-to-end integration tests`
  - Files: `tests/test_forecast_e2e.py`
  - Pre-commit: `python -m pytest tests/test_forecast.py tests/test_forecast_e2e.py -v`

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.

- [ ] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists (read file, curl endpoint, run command). For each "Must NOT Have": search codebase for forbidden patterns — reject with file:line if found. Check evidence files exist in .sisyphus/evidence/. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [ ] F2. **Code Quality Review** — `unspecified-high`
  Run `python -m py_compile` on all new files. Review all changed files for: unused imports, excessive comments, over-abstraction, generic names. Check AI slop. Verify all driver formulas are mathematically correct.
  Output: `Build [PASS/FAIL] | Files [N clean/N issues] | VERDICT`

- [ ] F3. **Real Manual QA** — `unspecified-high`
  Start from clean state. Execute EVERY QA scenario from EVERY task — follow exact steps, capture evidence. Test cross-task integration (MD&A extraction → driver proposal → forecast → sheet). Test edge cases: no segments, single segment, LLM failure. Save to `.sisyphus/evidence/final-qa/`.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [ ] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", read actual diff (git log/diff). Verify 1:1 — everything in spec was built (no missing), nothing beyond spec was built (no creep). Check "Must NOT do" compliance. Detect cross-task contamination. Flag unaccounted changes.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

- **1-3**: `feat(forecast): add MD&A extractor, data model, and LLM driver proposer` — mda_extractor.py, forecast_engine.py
- **4-6**: `feat(forecast): add forecast engine, CLI, and unit tests` — forecast_engine.py, tests/test_forecast.py
- **7-8**: `feat(forecast): add sheet renderer and pipeline integration` — forecast_sheet.py, run_pipeline.py
- **9**: `test(forecast): add end-to-end integration tests` — tests/test_forecast_e2e.py

---

## Success Criteria

### Verification Commands
```bash
python -m pytest tests/test_forecast.py tests/test_forecast_e2e.py -v  # All tests pass
python run_pipeline.py AAPL --skip-forecast  # Stages 1-4 unchanged
python forecast_engine.py --trees trees.json --mda mda.json -o forecast.json  # Valid JSON output
```

### Final Checklist
- [ ] MD&A extraction works for 10-K and 20-F filings
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
- [ ] No OpEx, BS, or CF forecasting
- [ ] No multi-scenario support
