# SEC Financial Modeling Pipeline - Gemini Context

This project is an automated, tree-based pipeline designed to automate the creation of financial models from SEC filings (10-K, 20-F). It uses a deterministic, XBRL tree-based parsing approach, minimizing reliance on LLMs.

## Core Philosophy

- **Python-First Computation**: All financial logic, modeling, and invariant checks happen in Python (`model/verify.py`). Google Sheets is strictly a display layer.
- **Deterministic-First**: XBRL parsing, linkbase merging, CIK resolution, and file downloads are pure Python standard library operations. LLMs are reserved for tasks requiring judgment (e.g., semantic reconciliation of invariant failures, fallback for non-XBRL filings, sibling grouping).
- **Three-Layer Merge Principle**: The system builds on a three-layer merge of XBRL linkbases: Calc layer (mathematical truth), Presentation layer (display order), and an "Other" layer (gap absorption).
- **Validation-Centric**: No model is output to a spreadsheet until all accounting invariants (e.g., Assets = Liabilities + Equity) are verified against the parsed trees.
- **Position over Names**: Financial statement structure is identified by tree position (e.g., BS_TA = Assets tree root), not by concept name matching.

## Pipeline Architecture

| Stage | Focus | Primary Script | Input | Output |
|-------|-------|----------------|-------|--------|
| **1** | Fetcher | `fetch/agent.py` | Ticker | SEC filing URLs |
| **2** | Builder | `xbrl/` module | URLs / iXBRL | `trees_{date}.json` |
| **3** | Merge | `merge/trees.py` | Multiple trees | `merged.json` |
| **4** | Verifier | `model/verify.py` | `merged.json` | Invariant checks |
| **5** | Sheet | `sheets/` module | `merged.json` | Final Google Sheet |
| **All** | Orchestrator| `run_pipeline.py` | Ticker | Final Google Sheet |

## Complete File Reference (47 .py files)

### Pipeline Stages (5 core files — root level)

| File | Lines | Role | Importable? |
|------|-------|------|-------------|
| `run_pipeline.py` | 145 | **Main orchestrator.** Runs all 5 stages end-to-end: fetch filings → build XBRL trees → merge → verify invariants → write Google Sheet. Entry point: `python run_pipeline.py AAPL`. Also used as import by `web/app.py`. | ✅ `run_pipeline()` |
| `fetch/agent.py` | 92 | **Stage 1: Filing Discovery.** Wraps `lookup_company` + `fetch_10k`/`fetch_20f` to resolve ticker → CIK → annual filing URLs. Returns JSON with all filing metadata. | ✅ `run()` |
| `xbrl/cli.py` | 94 | **Stage 2 CLI + Re-Export Hub.** Invokes `xbrl.build_statement_trees()`. Prints tree structures, finds groupable siblings, writes JSON. Also serves as import bridge — re-exports TreeNode, find_node_by_role, and all xbrl.* internals for other root files. | ✅ (re-exports xbrl internals) |
| `model/verify.py` | 223 | **Stage 3/4: Invariant Verification.** 7 cross-statement checks: BS balance, cash link, NI link, D&A link, SBC link, cash begin, segment sums. Falls back to `llm_invariant_fixer` when checks fail. | ✅ `verify_model()`, `run_checkpoint()`, `CheckpointResult` |
| `sheets/builder.py` | 26 | **Stage 5 CLI.** Thin wrapper: loads trees JSON → deserializes TreeNodes → calls `sheets.write_sheets()`. | CLI only |

### SEC Filing Discovery (3 files — root level)

| File | Lines | Role | Importable? |
|------|-------|------|-------------|
| `fetch/lookup.py` | 240 | Ticker/name → CIK resolution via SEC EDGAR. Determines domestic (10-K) vs foreign (20-F). Includes `search_tickers()` for substring search. Pure stdlib — no external deps. | ✅ All functions |
| `fetch/ten_k.py` | 104 | Fetches recent 10-K filing URLs for a given CIK from SEC submissions API. | ✅ `ticker_to_cik()`, `fetch_10k_filings()` |
| `fetch/twenty_f.py` | 97 | Same as above, but filters for 20-F filings (foreign private issuers). | ✅ `ticker_to_cik()`, `fetch_20f_filings()` |

### XBRL Engine (5 files — `xbrl/` package)

| File | Lines | Role |
|------|-------|------|
| `xbrl/__init__.py` | — | Package marker. Exports `build_statement_trees()` — the main entry point that orchestrates all XBRL parsing. |
| `xbrl/tree.py` | — | `TreeNode` class — the core data structure. Tree construction, role assignment, cascade layout, presentation ordering, period filtering, orphan fact supplementation, completeness verification. |
| `xbrl/linkbase.py` | — | Calc, presentation, and label linkbase fetchers and parsers. `parse_calc_linkbase()`, `parse_pre_linkbase()`, `parse_lab_linkbase()`, `classify_roles()`. |
| `xbrl/reconcile.py` | — | Cross-statement reconciliation. Role tagging (BS_TA, BS_TL, INC_NET, etc.), D&A/SBC identification, BS cash override, tree completeness verification, calc+pres merge algorithm. |
| `xbrl/segments.py` | — | Revenue segment decomposition. Detects dimensional breakdowns, attaches segment children to IS trees, builds revenue segment trees. |

### Legacy XBRL Path (1 file — root level)

| File | Lines | Role | Importable? |
|------|-------|------|-------------|
| `xbrl/facts_legacy.py` | 620 | **Legacy Stage 2b.** Parses iXBRL tags from HTML → maps to model codes (REVT, COGST, BS_TA, etc.) → builds structured financials JSON. Fallback for filings without calculation linkbase. The modern path uses `xbrl/` package instead. | ✅ All functions |

### Multi-Filing Merge (2 files — root level)

| File | Lines | Role | Importable? |
|------|-------|------|-------------|
| `merge/trees.py` | 239 | Merges multiple filing trees (newest first) into one unified tree with all historical periods. Handles concept renaming, orphan detection, reclassification fixes, and residual recomputation. | ✅ `merge_filing_trees()` |
| `merge/concepts.py` | 249 | Value-based concept alignment across filings. `ConceptMap` + `ConceptMatcher` classes. Detects parent-child reclassifications and sibling replacements. Used exclusively by `merge/trees.py`. | ✅ `ConceptMatcher`, `ConceptMap` |

### Shared Utilities (3 files — root level)

| File | Lines | Role | Importable? |
|------|-------|------|-------------|
| `fetch/http.py` | 110 | SEC EDGAR HTTP fetch with rate limiting (8 req/s), `.cache/` directory caching, offline fixture mode, recording mode, 5-retry backoff. The centralized fetch used by all SEC API calls. | ✅ `fetch_url()` |
| `llm/client.py` | 102 | Anthropic LLM helpers: code-fence stripping, truncated JSON recovery, `call_llm()` with retry. Used by `model/llm_fixer.py`. | ✅ `call_llm()`, `parse_json_response()` |
| `sheets/gws.py` | 47 | Google Sheets CLI wrappers: `gws_write()`, `gws_batch_update()` via `gws` subprocess. Used by `sheets/` package. | ✅ `gws_write()`, `gws_batch_update()` |

### LLM-Based Recovery (1 file — root level)

| File | Lines | Role | Importable? |
|------|-------|------|-------------|
| `model/llm_fixer.py` | 181 | LLM-in-the-loop semantic reconciliation. When `verify_model()` finds invariant errors, prompts Claude (Sonnet) to propose structural fixes (`move_role`, `change_weight`), applies them, and re-verifies. | ✅ `fix_invariants()` |

### Google Sheets Rendering (6 files — `sheets/` package)

| File | Lines | Role |
|------|-------|------|
| `sheets/__init__.py` | — | Package marker. Exports `write_sheets()` — the main entry point for sheet generation. |
| `sheets/api.py` | — | Google Sheets API interaction: spreadsheet creation, tab management, data writing. |
| `sheets/renderers.py` | — | Statement renderers: generates row arrays for IS, BS (Assets + L&E), and CF statements from TreeNode data. |
| `sheets/formatting.py` | — | Number format and style application via Sheets API `batchUpdate`. Currency, number, zero-dash formats, italic/bold styling. |
| `sheets/layouts.py` | — | Cascade layout algorithm, column ordering, row indentation by tree depth, "Other" row placement. |
| `sheets/formulas.py` | — | `=SUM()` formula generation from TreeNode children with signed weights. Cross-sheet cell references via global role map. |

### Debugging & Analysis Scripts (4 files — `scripts/` directory)

| File | Lines | Role |
|------|-------|------|
| `scripts/compare_views.py` | 156 | Compares calculation vs presentation linkbase ordering across all fixture companies. Reports mismatches per company. |
| `scripts/test_alignment.py` | 169 | Validates the 3-step merge algorithm (Match → Place → Other Gap) against fixture data. |
| `scripts/test_cascade.py` | 13 | Ad-hoc quick test: prints cascade layout for NFLX income statement tree. |
| `scripts/poc_reclassification.py` | 250 | **Temporary POC.** Detects and fixes TSLA revenue concept reclassification bug. Contains duplicated logic now integrated into `merge/concepts.py`. |

### Scripts (6 files — `scripts/` directory)

| File | Lines | Role |
|------|-------|------|
| `scripts/validate_10_companies.py` | — | Batch validation across 10 fixture companies. |
| `scripts/download_test_fixtures.py` | — | Downloads SEC filing data for offline test fixtures. |
| `scripts/compare_views.py` | 156 | Compares calc vs pres linkbase ordering across fixtures. |
| `scripts/test_alignment.py` | 169 | Validates merge algorithm against fixture data. |
| `scripts/test_cascade.py` | 13 | Quick cascade layout test for NFLX IS tree. |
| `scripts/poc_reclassification.py` | 250 | POC for TSLA revenue reclassification detection. |

### Test Suite (12 files — `tests/` directory)

| File | Role |
|------|------|
| `tests/__init__.py` | Test package marker. |
| `tests/test_dual_linkbase.py` | Dual linkbase (calc + pres) parsing unit tests. |
| `tests/test_merge_layers.py` | **Comprehensive merge test suite.** 9 synthetic tests + 8 real company fixtures. |
| `tests/test_merge_pipeline.py` | Multi-tree merge pipeline integration tests. |
| `tests/test_offline_e2e.py` | Offline end-to-end pipeline tests with fixture data. |
| `tests/test_sheet_formulas.py` | Google Sheets formula generation correctness tests. |
| `tests/test_da_sbc_tagging.py` | D&A and SBC concept identification tests. |
| `tests/test_model_historical.py` | Historical model computation tests. |
| `tests/test_demo_website.py` | Web demo API endpoint tests. |
| `tests/test_pymodel_units.py` | pymodel unit-level invariant check tests. |
| `tests/test_bs_cash_fix.py` | Balance Sheet cash fix verification. |
| `tests/test_reclassification.py` | Reclassification detection logic tests. |

### Web Demo (1 file — `web/` directory)

| File | Lines | Role |
|------|-------|------|
| `web/app.py` | — | FastAPI backend for local demo UI. Serves static files, proxies ticker search (via `lookup_company.search_tickers()`), and runs `run_pipeline.run_pipeline()` in a background thread. |

### File Count Summary

| Location | Count | Description |
|----------|-------|-------------|
| Root level | 15 | Pipeline stages, utilities (cleaned up from 21) |
| `xbrl/` | 5 | Core XBRL parsing engine |
| `sheets/` | 6 | Google Sheets rendering |
| `tests/` | 12 | Test suite (was 11, +1 from root) |
| `scripts/` | 6 | Dev tooling + debugging scripts (was 2, +4 from root) |
| `web/` | 1 | Demo website backend |
| **Total** | **45** | (was 47, −2: deleted `update_md.py`, merged duplicate sections) |

## Technical Standards

- **Models**: Use `claude-sonnet-4-6` for precision tasks and `claude-haiku-4-5-20251001` for high-volume grouping or text tasks.
- **Environment**: Use **Podman** for containerization (never Docker).
- **SEC Compliance**: Respect the SEC rate limit; use the `fetch_url` helper or built-in rate limiters.
- **Naming**: Use snake_case for Python identifiers and Title Case for spreadsheet labels.
- **Invariants**: Every model must pass the `verify_model()` checks in `model/verify.py` before final delivery.

## Common Workflows

```bash
# Full Pipeline Execution Example (Apple)
python run_pipeline.py AAPL

# Inspect tree structure
python xbrl/cli.py --url <filing_url> -o trees.json

# Check invariants without writing sheet
python model/verify.py --trees trees.json --checkpoint
```

## Maintenance Notes

- **Always use `run_pipeline.py`** to generate sheets. Running individual scripts bypasses the tree completeness gate and will produce sheets with broken formulas.
- If a company uses unique financial terminology, rely on the XBRL structure and grouping logic rather than hardcoded names.
- Always verify `gws` authentication before running Stage 5 (sheet writing).
