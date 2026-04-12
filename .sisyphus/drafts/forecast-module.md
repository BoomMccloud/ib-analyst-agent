# Draft: Forecast Module

## Requirements (confirmed)
- **Input**: Existing financial model (trees from xbrl_tree.py), financial nodes (MD&A text), historical financials
- **Process**: Analyze all revenue segmentations, identify growth drivers from MD&A
- **Driver types**: Always formulas — a * b (market share, units × ASP) or a * (1+r) growth rate
- **Output**: Both JSON forecast file + Google Sheet extension (forecast tabs + assumptions tab)
- **Forecast horizon**: Fixed 5 years
- **Driver extraction**: LLM-based (reads MD&A, proposes drivers)
- **Generic**: Must work for all companies/industries
- **Scope**: Revenue only (OpEx forecasted separately later)
- **MD&A sourcing**: Slice from filing HTML (heading-based detection)
- **Scenarios**: Single forecast only
- **Integration**: Stage 5 in run_pipeline.py

## Technical Decisions
- MD&A extraction: heading-based slicing from filing HTML (deterministic)
- Driver extraction: LLM reads MD&A + revenue segment data, proposes driver formulas
- Driver types: units×ASP, market_share×TAM, growth_rate, subscribers×ARPU
- Fallback: if LLM can't identify a driver, use historical CAGR
- Output format: JSON + Google Sheet extension (new "Forecast" tab + "Assumptions" tab)

## Research Findings

### Existing Data Structures
1. **TreeNode** (`xbrl_tree.py`): concept, tag, name, weight, values: {period: float}, children, is_leaf, role
2. **Revenue segments**: Already captured as `revenue_segments` TreeNode in trees JSON
3. **Sheet output**: 4 tabs (IS, BS, CF, Summary) with =SUM() formulas, cross-sheet refs
4. **MD&A nodes**: DO NOT EXIST — must be built
5. **Pipeline**: run_pipeline.py orchestrates stages 1-4. Stage 5 placeholder exists.

### Revenue Driver Patterns
- **Units × ASP**: Product companies
- **Subscribers × ARPU**: SaaS/subscription
- **Market size × Market share**: Competitive analysis
- **Prior × (1 + growth rate)**: Fallback

## Scope Boundaries
- INCLUDE: MD&A extraction, revenue driver identification, driver-based forecast, JSON + Sheet output
- EXCLUDE: OpEx forecasting, BS/CF forecasting, multi-scenario support
