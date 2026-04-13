# SEC Pipeline — Improvement Backlog

Evaluation performed 2026-04-12. Ordered by priority within each tier.

---

## P0 — High Impact / Low Effort

### 1. Fix Multi-Year Merge Validation Bugs
**[DONE]**
**What:** The 10-company validation run (`docs/todo/spec_multi_year_merge_bugs.md`) identified critical bugs preventing successful merges for TSLA (BS_CASH tagging failure), BRK-B (wrong hierarchy root), and JPM (duplicate facts).
**Impact:** Prevents correct model generation for major companies.
**What to do:** Implement the fixes described in `docs/todo/spec_multi_year_merge_bugs.md`.
**Resolution:** Reclassification detection (`us-gaap_Revenues` -> `us-gaap_RevenueFromContract...`) fixed by ensuring newest-first processing, skipping `__OTHER__` nodes in `base_index`, and retaining newest authoritative values. TSLA `TemporaryEquity` gap fixed by using `startswith("__OTHER__")` instead of `startswith("__")` so synthetic parent nodes don't get skipped in value merging.

---

### 2. Concept Matcher Refactor
**What:** The TSLA bug exposed that concept identification is fragile and scattered across `xbrl_tree.py`, `pymodel.py`, and `merge_trees.py` using ad-hoc matching strategies.
**Impact:** Causes tagging failures and makes adding new metrics difficult.
**What to do:** Extract all matching logic into a single `concept_matcher.py` module as detailed in `docs/todo/spec_concept_matcher.md`.

---

### 3. Complete Multi-Year Merge
**What:** The multi-year merge is mostly done but missing reclassification detection (Step 1c) and a hard gate for `verify_tree_completeness()`.
**Impact:** Silent errors can occur if historical numbers are restated.
**What to do:** Implement Tier 1 deterministic / Tier 2 LLM reclassification detection and enforce the completeness hard gate, per `docs/todo/spec_multi_year_merge.md`.

---

### 4. Extract shared utilities into `sec_utils.py` and `llm_utils.py`

**What:** `_throttle()`, `fetch_url()`, `HEADERS`, `REQUEST_INTERVAL` are copy-pasted across `fetch_10k.py`, `fetch_20f.py`, `lookup_company.py`. JSON code-fence stripping and truncation recovery logic is duplicated in `agent3_modeler.py`. `gws_write()`/`gws_batch_update()` appear in both spreadsheet files.

**Impact:** Any bug fix or behavior change (rate limit, retry logic, JSON recovery) must be applied in 3-4 places. Divergence between copies is inevitable and has likely already occurred. This is the single largest maintainability risk in the codebase.

**What to do:**
- Create `sec_utils.py` with `throttled_fetch()`, `HEADERS`, `REQUEST_INTERVAL`, and URL validation.
- Create `llm_utils.py` with `strip_code_fences()`, `recover_truncated_json()`, `call_llm()` wrapper.
- Create `gws_utils.py` with `gws_write()`, `gws_batch_update()`, `gws_clear()`.
- Update all consuming scripts to import from shared modules.
- Note: `fetch_10k.py`, `fetch_20f.py`, and `lookup_company.py` run inside Managed Agent containers and must remain self-contained. Only extract for scripts that run locally (stages 2a, 2b, 3, 4).

---

### 5. Replace fake SEC User-Agent email

**What:** All four fetch scripts use `"SecFilingsAgent admin@example.com"`. `admin@example.com` is an RFC 2606 example domain — no one receives email there. SEC EDGAR requires a real contact email and can IP-block violators.

**Impact:** If usage increases or SEC notices the pattern, all requests from this IP could be blocked. This is a compliance issue, not just a best practice.

**What to do:**
- Replace `admin@example.com` with a real contact email in `fetch_10k.py:17`, `fetch_20f.py:15`, `lookup_company.py:29`.
- After extracting `sec_utils.py` (item 4), this becomes a single-line change.
- Consider reading the email from an environment variable (`SEC_CONTACT_EMAIL`) so it's not hardcoded.

---

### 6. Add startup API key validation

**What:** Stages 2b, 3, and 4 use the Anthropic API but don't check for `ANTHROPIC_API_KEY` until the first API call.

**Impact:** Wasted time and confusing errors deep in a pipeline run. Users have to re-run after setting the key, losing progress on stages that don't checkpoint.

**What to do:**
- Add `if not os.environ.get("ANTHROPIC_API_KEY"): sys.exit("Error: ANTHROPIC_API_KEY not set")` at the top of `main()` in LLM-dependent files.
- Similarly validate `gws` is on PATH for Stage 4 scripts before doing any work.

---

## P1 — High Impact / Medium Effort

### 7. Revenue Forecasting Module (Stage 5)
**What:** The model currently only handles historical data. Analysts need a 5-year forecast for valuation modeling.
**Impact:** Essential for making the Google Sheet a complete valuation tool.
**What to do:** Implement Stage 5 as defined in `docs/todo/forecast-module.md`. This includes building a Two-Phase LLM Map-and-Slice extractor (`content_extractor.py`), a driver proposer (`forecast_engine.py`), and a sheet renderer (`forecast_sheet.py`).

---

### 9. Batch `gws` subprocess calls in Stage 4

**What:** Spreadsheets making one `subprocess.run(["gws", ...])` call per matched row.

**Impact:** Slow and wasteful. Each subprocess has ~200ms overhead (process spawn + auth).

**What to do:**
- Collect all value updates into a list, then issue a single `gws_batch_update()` call with all ranges.
- `gws_batch_update()` already exists and is used for formatting — extend its usage to data writes.

---

## P2 — Medium Impact / Medium Effort

### 12. Centralize configuration

**What:** Model names (`SONNET = "claude-sonnet-4-6"`, `HAIKU = "claude-haiku-4-5-20251001"`), rate limits (`REQUEST_INTERVAL = 1.0 / 8`), truncation limits (`max_chars`, `max_section_chars`), and SEC EDGAR URLs are all hardcoded in source across multiple files. No config file, no environment variable overrides.

**Impact:** Swapping models (e.g., when a new Sonnet version ships), adjusting rate limits, or changing truncation thresholds requires editing source code in multiple files. No way to configure per-environment (dev vs. prod).

**What to do:**
- Create `config.py` with all constants.
- Support environment variable overrides: `SEC_MODEL_PRECISION = os.environ.get("SEC_MODEL_PRECISION", "claude-sonnet-4-6")`.
- Import from `config.py` in all scripts.

---

### 13. Add prompt size guard in Stage 3

**What:** Sending JSON plus MD&A text to Sonnet in a single call with `max_tokens=16384` without size check.

**Impact:** If it exceeds the model's context window, the API call fails with an opaque error after the user has already waited.

**What to do:**
- Count approximate tokens before the API call (rough heuristic: `len(prompt) / 4`).
- If over 80% of context window, warn the user.
- If over 95%, truncate less-critical sections.

---

### 14. Validate SEC URLs

**What:** Taking raw URLs from the command line and passing directly to urllib without checking if it's from `sec.gov`.

**Impact:** A malformed URL from agent output or human error causes the script to fetch from arbitrary domains. This is an SSRF-adjacent risk in automated pipelines.

**What to do:**
- Add a check: `if not url.startswith("https://www.sec.gov/"): sys.exit("Error: URL must be from sec.gov")`.
- After extracting `sec_utils.py`, put this in the shared `fetch_url()` function.

---

### 15. Add structured logging

**What:** No file imports `logging`. All diagnostics go to `print(..., file=sys.stderr)`.

**Impact:** Cannot control verbosity without editing source. Cannot set different verbosity per stage. Makes debugging production issues harder.

**What to do:**
- Replace `print(stderr)` calls with `logging.info()` / `logging.warning()` / `logging.error()`.
- Configure log level via `--verbose` / `--quiet` CLI flags or `LOG_LEVEL` environment variable.

---

## P3 — Low Impact / Worth Noting

### 18. Cache `company_tickers.json` locally

**What:** `lookup_company.py` downloads the full SEC `company_tickers.json` (~1MB, 10K+ entries) on every invocation. The file changes infrequently.

**Impact:** Unnecessary network call and ~1s delay on every Stage 1 run.

**What to do:**
- Cache to a temp file with a 24-hour TTL.
- Check `os.path.exists()` and file mtime before fetching.

---

### 19. Cache filing HTML downloads

**What:** Downloading the full filing HTML on every run. A 10-K can be 10-20MB.

**Impact:** Re-running Stage 2a for testing or debugging means a fresh download every time + SEC rate limit delays.

**What to do:**
- Cache downloaded HTML to `./cache/<filing_hash>.html`.
- Add `--no-cache` flag to force re-download.

---

### 22. Add unit tests for core parsing logic
**[IN PROGRESS]**
New tests exist for tree-based logic (`test_model_historical.py`, `test_sheet_formulas.py`, etc.). Old LLM logic tests are no longer needed.

---

### 25. Add formatting for our model

**What:** The generated Google Sheet models lack styling.
**Impact:** It's hard for users to read the raw numbers without proper formatting (bolding headers, number formats, borders, etc).
**What to do:**
- Update `sheet_builder.py` to use `gws_batch_update()` for styling.
- Add bold text for subtotals, proper accounting formatting for numbers.

---

### 26. Support XBRL 1.1

**What:** Current pipeline only parses `_cal.xml` but companies like Microsoft use XBRL Calculation 1.1.
**Impact:** Cannot build models for MSFT and other modern filers.
**What to do:**
- Update `xbrl_tree.py` to parse `calculation-1.1.xsd` files in addition to `_cal.xml`.

---

## Architectural Improvements (Hybrid Approach)

### 27. Semantic Reconciliation Layer (LLM-in-the-Loop)
**What:** Introduce a dedicated LLM step when deterministic tree merging fails (e.g., due to restatements breaking exact-value overlaps).
**Impact:** Prevents massive `__OTHER__` residual rows and brittle merge failures by intelligently mapping concepts before resorting to catch-all balancing rows.
**What to do:**
- When `merge_trees.py` detects an unmapped concept or gap, isolate the discrepancy and prompt an LLM to determine the structural shift (e.g., 1-to-N split).
- Execute the LLM's mapping instruction instead of immediately failing or creating residuals.

---

### 28. Standardized Chart of Accounts (COA) Mapping
**What:** The pipeline extracts the exact tree structure defined by the company, resulting in bespoke model layouts for every company.
**Impact:** Makes cross-company comparisons (comparables) very difficult for analysts.
**What to do:**
- Define a Standardized Chart of Accounts (e.g., standard Revenue, COGS, SG&A, Operating Income).
- Use an LLM or deterministic dictionary to map the bespoke nodes from the raw XBRL tree into the standardized buckets.

---

### 29. Handle Restatements via "As-Reported" vs. "Latest-Available" Tracking
**What:** Attempting to perfectly merge conflicting historical realities (restatements) into a single tree breaks exact value matching.
**Impact:** Restated prior years (e.g., due to discontinued operations) destroy the merged tree's integrity.
**What to do:**
- Treat each filing as a separate "vintage".
- Trust the newest available vintage for any given year.
- Only use older filings to backfill years completely missing from the newer filings. Use the Semantic Reconciliation Layer to stitch them together if concepts don't perfectly align.

---

### 30. Soft Invariants vs. Hard Invariants
**What:** `pymodel.py` currently treats all 5 invariants as hard failures, including things like `NI Link` which often differ due to tagging quirks like Non-Controlling Interest (NCI).
**Impact:** Deterministic rules fail frequently across a large universe of companies due to semantic gaps in accounting concepts.
**What to do:**
- Differentiate between Hard Invariants (e.g., Assets == Liabilities + Equity) that must pass, and Soft Invariants (e.g., NI Link) that flag for review.
- Append a "Review Note" to the generated Google Sheet cell for Soft Invariant failures instead of halting the pipeline.
