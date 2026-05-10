# SEC Pipeline — Improvement Backlog

Evaluation updated 2026-04-13. Items are ordered by execution priority within each tier.

---

## P0 — Foundation, Reliability & Compliance (Must Do Next)

### 1. Persist LLM Semantic Fixes (Company Quirks Cache)
**What:** Save successful JSON patches (e.g., `move_role`, `change_weight`) from `model/llm_fixer.py` to a local `company_quirks.json` file.
**Impact:** Transforms expensive LLM calls into permanent, fast, deterministic rules. Saves API costs and ensures 100% success on re-runs.
**What to do:** Update fixer to save patches; update pipeline to apply them deterministically *before* validation.

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

### 8. LLM Provider Adapter Pattern (Strategy Pattern)
**What:** The pipeline is currently coupled to a single OpenAI-compatible provider via `llm/client.py`. We should abstract this into an `LLMProvider` interface with adapters for Anthropic, OpenAI, and Google (Gemini).
**Impact:** Prevents the entire pipeline from failing if a single provider is down or out of credits. Allows users to configure their preferred model (e.g., `SEC_LLM_PROVIDER=openai`).
**What to do:** Refactor `llm_utils.py` to use a Strategy Pattern. Define a standard interface that returns a parsed JSON dictionary, and implement `AnthropicAdapter`, `OpenAIAdapter`, etc. Control the active provider via an environment variable.

---

## P2 — Optimization & UX

### 9. Batch `gws` subprocess calls in Stage 4
**What:** Use `gws_batch_update()` for data writes instead of one subprocess call per matched row.
**Impact:** Significant performance improvement for large models.

### 11. Cache `company_tickers.json` and filing HTML
**What:** Cache SEC metadata and large HTML filings locally with TTL.
**Impact:** Reduces network dependency and speeds up re-runs during debugging.

---

## Architectural (Long-Term)

### 12. Standardized Chart of Accounts (COA) Mapping
Map bespoke XBRL nodes into standardized buckets (Revenue, COGS, etc.) to allow cross-company comparisons.

### 13. Handle Restatements via "As-Reported" vs. "Latest-Available" Tracking
Treat each filing as a separate "vintage" to correctly handle historical restatements without breaking tree integrity.

---

## Completed

*   **Repo cleanup for external sharing (2026-05-10)**:
    *   `fetch/lookup.py` — already imported from `fetch.http`; removed last unused `HEADERS` import.
    *   Renamed `xbrl/facts_legacy.py` → `xbrl/facts.py` (it's core, not legacy — used by `build_statement_trees()`).
    *   Deleted `test_phase1_e2e.sh` (referenced 4 deleted scripts; `tests/test_offline_e2e.py` is the real E2E test).
    *   Deleted root dev artifacts `spec_sheet.json` (6 MB) and `test.json` (124 KB).
    *   Merged `GEMINI.md` into `CLAUDE.md` (kept the "Three-Layer Merge Principle"); deleted GEMINI.md.
    *   `CLAUDE.md` — removed stale "Legacy paths" section (referenced 6 deleted scripts), updated facts reference, added validation-centric and three-layer merge principles to Architecture Notes.
    *   `README.md` — stripped "(was X.py)" rename annotations, fixed dead links (GEMINI.md, docs/backlog.md → docs/todo/backlog.md), removed stub for nonexistent `tests/test_model_historical_legacy.py`, fixed `pymodel.py`/`llm_invariant_fixer.py` references, removed stale "Two extraction paths" architecture note, fixed duplicated trailing line.
*   **P1 #7 — Startup environment validation**: Added `config.py` with `validate_environment()`. Both `run_pipeline.py main()` and `web/app.py` (at import time) now fail fast on missing `SEC_CONTACT_EMAIL` (or placeholder values), missing `gws` CLI, and warn on missing `LLM_API_KEY`. Offline mode (`SEC_OFFLINE_MODE=1`) skips email + gws checks for tests. (2026-05-10)
*   **P2 #10 — Centralize configuration**: Created `config.py` with `llm_*()` and `sec_*()` accessors plus `LLM_BASE_URL_DEFAULT`, `LLM_MODEL_DEFAULT`, `SEC_REQUEST_INTERVAL`. Replaced three different `SEC_CONTACT_EMAIL` fallbacks (one of them a real personal email) with a single rejection list. `llm/client.py`, `fetch/http.py`, `fetch/ten_k.py`, `fetch/twenty_f.py` all import from config. Removed dead duplicated `HEADERS`/warning-print code from `ten_k.py` and `twenty_f.py`. (2026-05-10)
*   **P0 #2 — Replace fake SEC User-Agent email**: All fetch scripts (`fetch/http.py`, `fetch/ten_k.py`, `fetch/twenty_f.py`, `fetch/lookup.py`) now read the User-Agent contact email from `SEC_CONTACT_EMAIL` env var. The fake `admin@example.com` has been removed from all code. Commit: `cc2f59f`.
*   **P0 #4 — Concept Matcher Refactor**: Concept identification logic consolidated from `xbrl_tree.py`, `pymodel.py`, and `merge_trees.py` into `merge/concepts.py` (249 lines, `ConceptMatcher` + `ConceptMap` classes). Imported by `merge/trees.py` and `tests/test_reclassification.py`. (Backlog file references were stale: the consolidated file is `merge/concepts.py`, not `concept_matcher.py`.)
*   **Refactor Fetch Scripts and Managed Agent**: Consolidated SEC EDGAR logic into `sec_utils.py` and refactored `agent1_fetcher.py` to run locally rather than using Anthropic Managed Agents, eliminating dependency and environment issues.
*   **Fix Multi-Year Merge Validation Bugs**: Resolved TSLA revenue reclassification and structural gap bugs.
*   **Semantic Reconciliation Layer (LLM-in-the-Loop)**: Implemented `llm_invariant_fixer.py` for self-healing semantic mismatches.
*   **Hard gate on `verify_tree_completeness()`**: Pipeline now halts on tree gaps.
