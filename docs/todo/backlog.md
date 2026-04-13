# SEC Pipeline — Improvement Backlog

Evaluation updated 2026-04-13. Items are ordered by execution priority within each tier.

---

## P0 — Foundation, Reliability & Compliance (Must Do Next)

### 1. Extract shared utilities into `sec_utils.py` and `llm_utils.py`
**What:** `_throttle()`, `fetch_url()`, `HEADERS`, `REQUEST_INTERVAL` are copy-pasted across `fetch_10k.py`, `fetch_20f.py`, `lookup_company.py`. JSON code-fence stripping and truncation recovery logic is duplicated in `agent3_modeler.py`. `gws_write()`/`gws_batch_update()` appear in both spreadsheet files.
**Impact:** Any bug fix or behavior change must be applied in 3-4 places. This is the single largest maintainability risk.
**What to do:** Create shared `sec_utils.py`, `llm_utils.py`, and `gws_utils.py` and update all consuming scripts.

### 2. Replace fake SEC User-Agent email
**What:** All four fetch scripts use `"SecFilingsAgent admin@example.com"`. SEC EDGAR requires a real contact email and can IP-block violators.
**Impact:** Critical compliance issue. Risk of IP-wide blocking.
**What to do:** Replace with real contact email (via env var `SEC_CONTACT_EMAIL`).

### 3. Persist LLM Semantic Fixes (Company Quirks Cache)
**What:** Save successful JSON patches (e.g., `move_role`, `change_weight`) from `llm_invariant_fixer.py` to a local `company_quirks.json` file.
**Impact:** Transforms expensive LLM calls into permanent, fast, deterministic rules. Saves API costs and ensures 100% success on re-runs.
**What to do:** Update fixer to save patches; update pipeline to apply them deterministically *before* validation.

### 4. Concept Matcher Refactor
**What:** Concept identification logic is fragile and scattered across `xbrl_tree.py`, `pymodel.py`, and `merge_trees.py`.
**Impact:** Causes tagging failures and makes adding new metrics difficult.
**What to do:** Extract all matching logic into a single `concept_matcher.py` module.

---

## P1 — Critical Logic & Major Features

### 5. Revenue Forecasting Module (Stage 5)
**What:** Implement 5-year revenue forecasting based on MD&A text analysis and segment growth drivers.
**Impact:** Essential for making the Google Sheet a complete valuation tool.
**What to do:** Build `content_extractor.py`, `forecast_engine.py`, and `forecast_sheet.py` as defined in `docs/todo/forecast-module.md`.

### 6. Soft Invariants vs. Hard Invariants
**What:** Structurally distinguish between mathematical truths (`BS Balance`) and semantic mapping failures (`NI Link`).
**Impact:** Ensures mathematical errors halt the pipeline while semantic errors are routed to the self-healing LLM fixer.
**What to do:** Update `pymodel.py` to bifurcate error handling based on invariant type.

### 7. Add startup API key and environment validation
**What:** Fail fast if `ANTHROPIC_API_KEY` or `gws` are missing from the environment.
**Impact:** Prevents wasted time and confusing errors deep in a pipeline run.

---

## P2 — Optimization & UX

### 8. Batch `gws` subprocess calls in Stage 4
**What:** Use `gws_batch_update()` for data writes instead of one subprocess call per matched row.
**Impact:** Significant performance improvement for large models.

### 9. Centralize configuration
**What:** Move model names, rate limits, and thresholds to `config.py` with environment variable overrides.
**Impact:** Simplifies maintenance and environment-specific tuning.

### 10. Cache `company_tickers.json` and filing HTML
**What:** Cache SEC metadata and large HTML filings locally with TTL.
**Impact:** Reduces network dependency and speeds up re-runs during debugging.

---

## Architectural (Long-Term)

### 11. Standardized Chart of Accounts (COA) Mapping
Map bespoke XBRL nodes into standardized buckets (Revenue, COGS, etc.) to allow cross-company comparisons.

### 12. Handle Restatements via "As-Reported" vs. "Latest-Available" Tracking
Treat each filing as a separate "vintage" to correctly handle historical restatements without breaking tree integrity.

---

## Completed

*   **Fix Multi-Year Merge Validation Bugs**: Resolved TSLA revenue reclassification and structural gap bugs.
*   **Semantic Reconciliation Layer (LLM-in-the-Loop)**: Implemented `llm_invariant_fixer.py` for self-healing semantic mismatches.
*   **Hard gate on `verify_tree_completeness()`**: Pipeline now halts on tree gaps.
