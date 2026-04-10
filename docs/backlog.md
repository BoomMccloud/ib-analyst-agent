# SEC Pipeline — Improvement Backlog

Evaluation performed 2026-04-10. Ordered by priority within each tier.

---

## P0 — High Impact / Low Effort

### 0. Restructure Pipeline into Decoupled 2-Phase State Machine

**What:** The current pipeline is a monolithic forward-pass that tangles historical extraction and future forecasting. If a forecast assumption breaks the model, the entire run (including expensive historical parsing) crashes.

**Impact:** Decoupling the pipeline into two strict, isolated phases creates a "save point" for historical data, isolates errors to the exact phase that caused them, and allows for instant re-running of forecasts without re-parsing raw SEC HTML.

**What to do:**
Break the pipeline into two strict phases:

**Phase 1: Establish the "Ground Truth" (Filings → Verified Historical Baseline)**
- **Strict Data Contracts:** Use Pydantic models and Anthropic Structured Outputs (Tool Use) to force the LLM to extract historical line items into a mathematically rigid schema.
- **Strict Annual Periods (12-Month Filtering):** The pipeline defaults to 5 years. Force the extraction and Python engine to ignore partial-year or trailing-twelve-month (TTM) columns that often appear in filings, strictly populating the baseline only when full fiscal year data is available.
- **Dynamic Chart of Accounts:** Treat SEC categories (e.g., Total Current Assets) as immutable parent totals. Allow the LLM to extract dynamic, company-specific child line items.
- **Python Physics Engine:** Python maps these items, computes mathematical "Catch-Alls" (`Parent Total - Sum(Child Items)`) to tolerate minor LLM omissions, and verifies historical invariants (Assets = L+E).
- **Historical Reconciliation Loop:** If the historical math breaks, Python identifies the exact period/statement and loops back to the LLM ("Assets are $5B higher than L+E in 2023. Re-examine the text.") until the historical base is perfectly balanced.
- **Output:** A mathematically perfect `historical_baseline.json`.

**Phase 2: The Forecast Layer (Historical Baseline + MD&A → Future Model)**
- **Forecast Generation:** A separate process reads `historical_baseline.json` and the MD&A. The LLM acts purely as a forecaster, generating base assumptions (growth rates, unit economics). *The LLM is forbidden from doing arithmetic.*
- **Sanity Checks & Feedback:** Python applies these drivers to the historical base to project the future. Python then calculates "Sanity Checks" (e.g., Implied Gross Margin, Implied Headcount). If sanity checks deviate wildly, Python triggers a loop back to the forecasting LLM ("Your unit growth assumption implies an impossible 90% gross margin. Revise.")
- **Output Generation:** Python writes the combined data to Google Sheets, appending new "Rev Build", "Expense Build", and standalone "Sanity Checks" tabs. The generated sheet MUST retain live dynamic formulas and inline invariant checks so end-users can tweak assumptions seamlessly.

---

### 1. Extract shared utilities into `sec_utils.py` and `llm_utils.py`

**What:** `_throttle()`, `fetch_url()`, `HEADERS`, `REQUEST_INTERVAL` are copy-pasted across `fetch_10k.py`, `fetch_20f.py`, `lookup_company.py`, and `extract_sections.py`. JSON code-fence stripping and truncation recovery logic is duplicated in `structure_financials.py`, `agent3_modeler.py`, and `build_model_sheet.py`. `gws_write()`/`gws_batch_update()` appear in both spreadsheet files. `_flatten_bs()`, `_flatten_cf()`, `BS_CODE_DEFS`, and `CF_CODE_DEFS` are ~150 lines duplicated verbatim between `structure_financials.py` and `build_model_sheet.py`.

**Impact:** Any bug fix or behavior change (rate limit, retry logic, JSON recovery) must be applied in 3-4 places. Divergence between copies is inevitable and has likely already occurred. This is the single largest maintainability risk in the codebase.

**What to do:**
- Create `sec_utils.py` with `throttled_fetch()`, `HEADERS`, `REQUEST_INTERVAL`, and URL validation.
- Create `llm_utils.py` with `strip_code_fences()`, `recover_truncated_json()`, `call_llm()` wrapper.
- Create `gws_utils.py` with `gws_write()`, `gws_batch_update()`, `gws_clear()`.
- Create `financial_utils.py` with `_flatten_bs()`, `_flatten_cf()`, `BS_CODE_DEFS`, `CF_CODE_DEFS`.
- Update all consuming scripts to import from shared modules.
- Note: `fetch_10k.py`, `fetch_20f.py`, and `lookup_company.py` run inside Managed Agent containers and must remain self-contained. Only extract for scripts that run locally (stages 2a, 2b, 3, 4).

---

### 2. Replace fake SEC User-Agent email

**What:** All four fetch scripts use `"SecFilingsAgent admin@example.com"`. `admin@example.com` is an RFC 2606 example domain — no one receives email there. SEC EDGAR requires a real contact email and can IP-block violators.

**Impact:** If usage increases or SEC notices the pattern, all requests from this IP could be blocked. This is a compliance issue, not just a best practice.

**What to do:**
- Replace `admin@example.com` with a real contact email in `fetch_10k.py:17`, `fetch_20f.py:15`, `lookup_company.py:29`, `extract_sections.py:27`.
- After extracting `sec_utils.py` (item 1), this becomes a single-line change.
- Consider reading the email from an environment variable (`SEC_CONTACT_EMAIL`) so it's not hardcoded.

---

### 3. Add startup API key validation

**What:** Stages 2b, 3, and 4 use the Anthropic API but don't check for `ANTHROPIC_API_KEY` until the first API call. Stage 2a can run for 10+ minutes downloading and parsing HTML before Stage 2b discovers the key is missing.

**Impact:** Wasted time and confusing errors deep in a pipeline run. Users have to re-run after setting the key, losing progress on stages that don't checkpoint.

**What to do:**
- Add `if not os.environ.get("ANTHROPIC_API_KEY"): sys.exit("Error: ANTHROPIC_API_KEY not set")` at the top of `main()` in `structure_financials.py`, `agent3_modeler.py`, `agent4_spreadsheet.py`, and `build_model_sheet.py`.
- Similarly validate `gws` is on PATH for Stage 4 scripts before doing any work.

---

## P1 — High Impact / Medium Effort

### 4. Parallelize Stage 2b LLM calls

**What:** `structure_financials.py` processes 7-8 filing sections sequentially in a `for` loop (line 562). IS, BS, CF sections go to Sonnet; notes and MD&A go to Haiku. All calls are independent of each other. Classification calls (`classify_model_codes()` at line 493) are also serialized.

**Impact:** Stage 2b is the slowest stage in the pipeline. Each LLM call takes 5-30 seconds. Sequential processing means 7 calls = 35-210 seconds wall-clock. Parallelizing to 4 workers would reduce this to ~2-3 calls worth of time, a **3-5x speedup**.

**What to do:**
- Wrap section processing in `concurrent.futures.ThreadPoolExecutor(max_workers=4)`.
- Each section's `call_llm()` is already stateless — no shared mutable state to worry about.
- Keep `print(stderr)` calls thread-safe (they already are in CPython due to GIL, but consider using `logging` — see item 11).
- Similarly parallelize the classification calls.

---

### 5. Batch `gws` subprocess calls in Stage 4

**What:** `agent4_spreadsheet.py`'s `populate_annual()` makes one `subprocess.run(["gws", ...])` call per matched row — up to 30+ subprocess invocations per filing. Each spawns a new process, authenticates, and makes one Sheets API call.

**Impact:** Slow and wasteful. Each subprocess has ~200ms overhead (process spawn + auth). 30 calls = 6+ seconds of pure overhead. A single `batchUpdate` with all value ranges would complete in one round trip.

**What to do:**
- Collect all value updates into a list, then issue a single `gws_batch_update()` call with all ranges.
- `gws_batch_update()` already exists and is used for formatting — extend its usage to data writes.
- This also fixes the partial-write problem: if the batch fails, no data is written (vs. current behavior where a mid-sequence failure leaves the sheet half-populated).

---

### 6. Deprecate `agent4_spreadsheet.py`

**What:** Two spreadsheet implementations exist. `agent4_spreadsheet.py` is template-based with hardcoded row numbers (lines 138-167 map to literal integers like `48`, `51`, `55`). `build_model_sheet.py` is code-driven with SUMIF formulas and short codes. The CLAUDE.md already acknowledges `build_model_sheet.py` is "more robust and maintainable."

**Impact:** Maintaining two implementations doubles the surface area for bugs. The template-based approach has known issues: dead `label_to_row` code (built but never used at line 103), unlimited recursion in `_exact_search()` (line 207), and hardcoded row numbers that silently break if the template changes. New contributors don't know which one to use.

**What to do:**
- Archive `agent4_spreadsheet.py` (move to `archive/` or delete).
- Remove `template_row_map.json` and `create_template.py` if they're only used by the template-based approach.
- Update CLAUDE.md to remove the "two options" section.
- If keeping for reference, add a prominent deprecation comment at the top.

---

### 7. Remove dead code in `agent4_spreadsheet.py`

**What:** `label_to_row` dict (line 103) is built from the template row_map but is never used to resolve any of the three mapping dicts (`is_mapping`, `cf_mapping`, `bs_mapping`). The mappings use hardcoded integers instead.

**Impact:** Misleading code. A developer reading the code would assume `label_to_row` drives the row resolution, when in fact it does nothing. This makes the template approach appear more dynamic than it is.

**What to do:**
- If deprecating `agent4_spreadsheet.py` (item 6), this is moot.
- If keeping it, either wire `label_to_row` into the mapping resolution (making it actually dynamic) or delete it.

---

## P2 — Medium Impact / Medium Effort

### 8. Centralize configuration

**What:** Model names (`SONNET = "claude-sonnet-4-6"`, `HAIKU = "claude-haiku-4-5-20251001"`), rate limits (`REQUEST_INTERVAL = 1.0 / 8`), truncation limits (`max_chars`, `max_section_chars`), and SEC EDGAR URLs are all hardcoded in source across multiple files. No config file, no environment variable overrides.

**Impact:** Swapping models (e.g., when a new Sonnet version ships), adjusting rate limits, or changing truncation thresholds requires editing source code in multiple files. No way to configure per-environment (dev vs. prod).

**What to do:**
- Create `config.py` with all constants.
- Support environment variable overrides: `SEC_MODEL_PRECISION = os.environ.get("SEC_MODEL_PRECISION", "claude-sonnet-4-6")`.
- Import from `config.py` in all scripts.

---

### 9. Add prompt size guard in Stage 3

**What:** `agent3_modeler.py` sends the full structured financials JSON (potentially hundreds of KB for multi-year, multi-segment companies) plus MD&A text to Sonnet in a single call with `max_tokens=16384` (line 229). No size check before the call.

**Impact:** For large companies with 5+ years of detailed segment data, the prompt can reach 100-200K tokens. If it exceeds the model's context window, the API call fails with an opaque error after the user has already waited through stages 1-2.

**What to do:**
- Count approximate tokens before the API call (rough heuristic: `len(prompt) / 4`).
- If over 80% of context window, warn the user.
- If over 95%, truncate less-critical sections (e.g., older years, detailed notes) with a clear message.
- Consider splitting very large inputs across multiple calls with a merge step.

---

### 10. Validate SEC URLs in `extract_sections.py`

**What:** `extract_sections.py` takes a raw URL from the command line and passes it directly to `urllib.request.urlopen()` (line 251). No validation that the URL is from `sec.gov`.

**Impact:** A malformed URL from agent output or human error causes the script to fetch from arbitrary domains. This is an SSRF-adjacent risk in automated pipelines.

**What to do:**
- Add a check: `if not url.startswith("https://www.sec.gov/"): sys.exit("Error: URL must be from sec.gov")`.
- After extracting `sec_utils.py`, put this in the shared `fetch_url()` function.

---

### 11. Add structured logging

**What:** No file imports `logging`. All diagnostics go to `print(..., file=sys.stderr)`. Works for manual CLI usage but limits observability.

**Impact:** Cannot control verbosity without editing source. Cannot redirect logs separately from JSON output (which goes to stdout). Cannot set different verbosity per stage. Makes debugging production issues harder.

**What to do:**
- Replace `print(stderr)` calls with `logging.info()` / `logging.warning()` / `logging.error()`.
- Configure log level via `--verbose` / `--quiet` CLI flags or `LOG_LEVEL` environment variable.
- Use a consistent format: `%(asctime)s %(levelname)s %(name)s %(message)s`.
- Keep JSON output on stdout, logs on stderr (this is already the convention, just formalize it).

---

### 12. Normalize cash flow format at the source

**What:** The LLM in Stage 2b sometimes outputs cash flow data in "section-first" format (keys are `operating_activities`, `investing_activities`, etc.) instead of "period-first" format, despite explicit prompting. Both `build_model_sheet.py` (line 414, `_convert_section_first()`) and `structure_financials.py` (line 431, `_detect_cf_periods()`) have conversion functions to handle this ambiguity.

**Impact:** Format ambiguity propagates through the pipeline. Each downstream consumer must handle both formats, duplicating conversion logic.

**What to do:**
- Add a normalization step at the end of Stage 2b's cash flow processing that always converts to period-first format.
- Remove the conversion functions from downstream stages.
- Add a retry with a more explicit prompt if the LLM outputs the wrong format.

---

### 13. Handle `[TRUNCATED]` marker in LLM prompts

**What:** `extract_sections.py` appends `"\n\n[TRUNCATED]"` (line 298) to sections exceeding `max_section_chars`. This string is fed to the LLM in Stage 2b without any mention in the prompt.

**Impact:** The LLM may not recognize `[TRUNCATED]` as a system marker. It could treat it as literal content, ignore it, or hallucinate data for the missing portion. Financial data from truncated sections may be incomplete without any downstream warning.

**What to do:**
- Add to the Stage 2b prompt: "If the input ends with [TRUNCATED], the section was cut short. Extract only the data that is present. Do not infer or fabricate missing data. Include a `truncated: true` flag in your output."
- Propagate the `truncated` flag through stages so the final spreadsheet can mark affected periods.

---

## P3 — Low Impact / Worth Noting

### 14. Cache `company_tickers.json` locally

**What:** `lookup_company.py` downloads the full SEC `company_tickers.json` (~1MB, 10K+ entries) on every invocation (line 68). The file changes infrequently (new tickers are rare).

**Impact:** Unnecessary network call and ~1s delay on every Stage 1 run. In the Managed Agent container there's no persistent storage, but for local dev/testing this adds up.

**What to do:**
- Cache to a temp file with a 24-hour TTL.
- Check `os.path.exists()` and file mtime before fetching.
- For Managed Agent runs, this won't help (ephemeral filesystem), but local runs benefit.

---

### 15. Cache filing HTML downloads

**What:** `extract_sections.py` downloads the full filing HTML on every run (line 251). A 10-K can be 10-20MB.

**Impact:** Re-running Stage 2a for testing or debugging means a fresh download every time + SEC rate limit delays.

**What to do:**
- Cache downloaded HTML to `./cache/<filing_hash>.html`.
- Add `--no-cache` flag to force re-download.
- Respect SEC rate limits on cache misses.

---

### 16. Fix `_flatten_bs()` inner function scoping

**What:** In both `structure_financials.py` (line 376) and `build_model_sheet.py` (line 273), the inner `def collect(data, section_path)` is defined inside a `for period in periods:` loop, causing it to be redefined on every iteration.

**Impact:** No functional bug (Python closures handle this correctly), but it's an unusual pattern that confuses readers and subtly depends on Python scoping rules.

**What to do:**
- Move `def collect()` outside the loop body.
- After extracting to `financial_utils.py` (item 1), fix it once.

---

### 17. Add recursion depth limit to `_exact_search()`

**What:** `agent4_spreadsheet.py`'s `_exact_search()` (line 196) recurses into nested dicts without a depth limit.

**Impact:** Deeply nested LLM output could cause a stack overflow. Unlikely in practice but a defensive coding gap.

**What to do:**
- Add a `max_depth` parameter (default 10).
- Return `None` if depth exceeded.
- Moot if `agent4_spreadsheet.py` is deprecated (item 6).

---

### 18. Add unit tests for core parsing logic

**What:** Zero test files exist. Complex, brittle logic in `_flatten_bs()`, `_flatten_cf()`, `_convert_section_first()`, `html_to_text()`, `extract_toc()`, `match_toc_to_sections()`, `_extract_json()`, and the JSON truncation recovery is entirely untested.

**Impact:** Regression risk on any refactor. The JSON recovery and extraction logic has known edge cases (brace counting inside strings, regex matching prompt echo-back) that would be caught by tests.

**What to do:**
- Create `tests/` directory.
- Start with the highest-risk functions:
  - `_extract_json()` — test with multi-candidate responses, prompt echo-back, malformed JSON.
  - `recover_truncated_json()` — test with braces inside string values, nested objects, arrays.
  - `_flatten_bs()` / `_flatten_cf()` — test with period-first and section-first formats.
  - `html_to_text()` — test with complex table structures, entity expansions.
- Use `pytest`. No mocking of the LLM needed for these — they're pure functions.

---

### 19. Headcount extraction heuristic

**What:** `extract_sections.py`'s `extract_headcount()` (line 235) picks `max(results, key=lambda r: r["count"])`, assuming the largest number is total headcount.

**Impact:** For conglomerates or companies reporting peak seasonal headcount alongside year-end headcount, this heuristic returns the wrong number with no warning.

**What to do:**
- Add context-aware filtering: prefer numbers near keywords like "total", "approximately", "as of [fiscal year end]".
- If multiple candidates are close in magnitude, flag ambiguity in the output.
- Low priority — headcount is used for model context, not financial calculations.