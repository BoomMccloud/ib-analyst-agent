# File Map — sec-agent

> Auto-generated audit of all `.py` files in the repository.
> Use this to find what you need: which file does what, where it should live, and how it connects to others.

---

## Root-Level `.py` Files (15 files — cleaned up from 21)

### 🟢 Core Pipeline (5 files) — Entry points + orchestrator

| File | Lines | Function | Recommended Location | Depends On |
|------|-------|----------|---------------------|------------|
| `run_pipeline.py` | 145 | **Main orchestrator.** Runs all 5 stages: fetch → build trees → merge → verify → write sheet. Entry: `python run_pipeline.py AAPL`. Also importable for web demo. | **Stay at root** (public entry point) | `agent1_fetcher`, `sec_utils`, `xbrl.*`, `merge_trees`, `pymodel`, `sheets.*` |
| `agent1_fetcher.py` | 92 | **Stage 1: Filing fetcher.** Wraps `lookup_company` + `fetch_10k`/`fetch_20f` to discover annual filings for a ticker. | `fetchers/agent.py` | `lookup_company`, `fetch_10k`, `fetch_20f` |
| `xbrl_tree.py` | 94 | **Stage 2 CLI + re-export hub.** Invokes `xbrl.build_statement_trees()`. Prints trees, finds groupable siblings, writes JSON. Also the import bridge for `xbrl.*` internals. | `xbrl/cli.py` (CLI part); re-exports belong in `xbrl/__init__.py` | `sec_utils`, `xbrl.tree`, `xbrl.linkbase`, `xbrl.segments`, `xbrl.reconcile` |
| `pymodel.py` | 223 | **Stage 3: Invariant verification.** 7 cross-statement checks (BS balance, cash link, NI link, D&A link, SBC link, cash begin, segment sums). Fallback to `llm_invariant_fixer` on failure. | `model/verify.py` | `xbrl_tree` (TreeNode, find_node_by_role), `llm_invariant_fixer` |
| `sheet_builder.py` | 26 | **Stage 4: Sheet CLI.** Thin wrapper that loads trees JSON → `sheets.write_sheets()`. | `sheets/cli.py` | `sheets.*`, `xbrl_tree` (TreeNode) |

### 🔵 SEC Filing Discovery (3 files) — Stage 1 helpers

| File | Lines | Function | Recommended Location | Depends On |
|------|-------|----------|---------------------|------------|
| `lookup_company.py` | 240 | Ticker/name → CIK resolution via SEC EDGAR. Determines domestic (10-K) vs foreign (20-F). Also: `search_tickers()` for substring search. | `fetchers/lookup.py` | stdlib only (urllib) |
| `fetch_10k.py` | 104 | Fetches recent 10-K filing URLs for a CIK. | `fetchers/fetch_10k.py` | `sec_utils` |
| `fetch_20f.py` | 97 | Fetches recent 20-F filing URLs for foreign issuers. | `fetchers/fetch_20f.py` | `sec_utils` |

### 🟡 XBRL Parsing (1 file) — Legacy fallback

| File | Lines | Function | Recommended Location | Depends On |
|------|-------|----------|---------------------|------------|
| `parse_xbrl_facts.py` | 620 | **Legacy Stage 2b.** Parses iXBRL tags from HTML, maps them to model codes (REVT, COGST, etc.), builds structured financials JSON. Fallback for filings without calculation linkbase. The modern path uses `xbrl/` package instead. | `xbrl/parse_facts.py` | stdlib + `re` |

### 🟠 Merging & Alignment (2 files) — Stage 3 multi-filing

| File | Lines | Function | Recommended Location | Depends On |
|------|-------|----------|---------------------|------------|
| `merge_trees.py` | 239 | Merges multiple filing trees (newest first) into a single tree with all historical periods. Handles concept renaming, orphan detection, reclassification fixes, and residual recomputation. | `model/merge.py` | `xbrl_tree` (TreeNode), `concept_matcher` |
| `concept_matcher.py` | 249 | Value-based concept alignment across filings. Detects parent-child reclassifications and sibling replacements. Used ONLY by `merge_trees.py`. | `model/concept_matcher.py` | `xbrl_tree` types |

### 🟣 Shared Utilities (3 files) — Used across pipeline

| File | Lines | Function | Recommended Location | Depends On |
|------|-------|----------|---------------------|------------|
| `sec_utils.py` | 110 | SEC EDGAR HTTP fetch with rate limiting (8 req/s), caching (`.cache/`), offline fixture mode, recording mode. Centralized fetch with 5-retry backoff. | `utils/sec.py` | stdlib (urllib, hashlib) |
| `llm_utils.py` | 102 | Anthropic LLM helpers: code-fence stripping, truncated JSON recovery, `call_llm()` with retry. | `utils/llm.py` | `anthropic` |
| `gws_utils.py` | 47 | Google Sheets CLI wrappers: `gws_write()`, `gws_batch_update()` via `gws` subprocess. Used by `sheets/` package. | `sheets/gws.py` (or `utils/gws.py`) | `subprocess` |

### 🔴 LLM Fixer (1 file) — Semantic reconciliation

| File | Lines | Function | Recommended Location | Depends On |
|------|-------|----------|---------------------|------------|
| `llm_invariant_fixer.py` | 181 | LLM-in-the-loop invariant fixer. When `verify_model()` fails, prompts Claude (Sonnet) to propose structural fixes (`move_role`, `change_weight`), applies them, and re-verifies. | `model/invariant_fixer.py` | `anthropic`, `llm_utils`, `xbrl_tree`, `pymodel` |

### ⚪ Debugging/Analysis Scripts (0 files here — moved to `scripts/`)

All debugging and analysis scripts have been moved to `scripts/`:
- `scripts/compare_views.py` — Compares calc vs presentation linkbase ordering
- `scripts/test_alignment.py` — Validates 3-step merge algorithm
- `scripts/test_cascade.py` — Quick cascade layout test
- `scripts/poc_reclassification.py` — TSLA reclassification POC

### ⚫ Disposable (0 files — all cleaned up)

| File | Status |
|------|--------|
| `update_md.py` | ✅ **Deleted** — one-time text replacement script, already executed. |
| `poc_reclassification.py` | ✅ **Moved** to `scripts/poc_reclassification.py` |

---

## Files Already in Proper Subdirectories (25 files — OK)

### `tests/` (12 files)
| File | Purpose |
|------|---------|
| `tests/__init__.py` | Package marker |
| `tests/test_dual_linkbase.py` | Dual linkbase parsing unit tests |
| `tests/test_merge_pipeline.py` | Multi-tree merge pipeline tests |
| `tests/test_offline_e2e.py` | Offline end-to-end pipeline tests |
| `tests/test_sheet_formulas.py` | Google Sheets formula generation tests |
| `tests/test_da_sbc_tagging.py` | D&A/SBC tag identification tests |
| `tests/test_model_historical.py` | Historical model computation tests |
| `tests/test_demo_website.py` | Web demo API tests |
| `tests/test_pymodel_units.py` | pymodel unit tests |
| `tests/test_bs_cash_fix.py` | BS Cash fix verification |
| `tests/test_reclassification.py` | Reclassification detection tests |
| `tests/test_merge_layers.py` | ✅ **Moved here from root.** Comprehensive merge test suite (9 synthetic + 8 real companies). |

### `sheets/` (6 files)
| File | Purpose |
|------|---------|
| `sheets/__init__.py` | Package marker + `write_sheets()` export |
| `sheets/renderers.py` | Statement renderers (IS, BS, CF rows) |
| `sheets/formatting.py` | Number format + style application |
| `sheets/layouts.py` | Cascade layout + column ordering |
| `sheets/formulas.py` | `=SUM()` formula generation |
| `sheets/api.py` | Google Sheets API interaction |

### `xbrl/` (5 files)
| File | Purpose |
|------|---------|
| `xbrl/__init__.py` | `build_statement_trees()` export |
| `xbrl/tree.py` | `TreeNode` class + tree operations |
| `xbrl/linkbase.py` | Calc/presentation/label linkbase parsers |
| `xbrl/reconcile.py` | Cross-statement reconciliation + role tagging |
| `xbrl/segments.py` | Revenue segment decomposition |

### `scripts/` (2 files)
| File | Purpose |
|------|---------|
| `scripts/validate_10_companies.py` | Batch validation across companies |
| `scripts/download_test_fixtures.py` | Test fixture downloader |

### `web/` (1 file)
| File | Purpose |
|------|---------|
| `web/app.py` | FastAPI demo backend |

---

## Cleanup Status (May 2026)

### ✅ Completed
1. **Deleted `update_md.py`** — one-time script, already executed.
2. **`test_merge_layers.py`** → `tests/test_merge_layers.py` — 635-line test suite now in proper location.
3. **`compare_views.py`** → `scripts/compare_views.py`
4. **`test_alignment.py`** → `scripts/test_alignment.py`
5. **`test_cascade.py`** → `scripts/test_cascade.py` (fixed wrong import: `sheet_builder` → `sheets.layouts`)
6. **`poc_reclassification.py`** → `scripts/poc_reclassification.py`

### 🔮 Future (Low Priority — structural refactor with import updates)

| Package | Files to move | Risk |
|---------|--------------|------|
| `fetchers/` | `lookup_company.py`, `fetch_10k.py`, `fetch_20f.py`, `agent1_fetcher.py` | Low (4 importers to update) |
| `model/` | `pymodel.py`, `merge_trees.py`, `concept_matcher.py`, `llm_invariant_fixer.py` | Medium (xbrl_tree importers) |
| `utils/` | `sec_utils.py`, `llm_utils.py` | Medium (5 importers for sec_utils) |
| `xbrl/` | `parse_xbrl_facts.py` (legacy) | Low (no importers) |
| `sheets/` | `gws_utils.py` → `sheets/gws.py`, `sheet_builder.py` → `sheets/cli.py` | Low |

---

## Dependency Graph (Simplified)

```
run_pipeline.py
  ├── agent1_fetcher.py
  │     ├── lookup_company.py
  │     ├── fetch_10k.py ──→ sec_utils.py
  │     └── fetch_20f.py ──→ sec_utils.py
  ├── sec_utils.py
  ├── xbrl/ (__init__, tree, linkbase, reconcile, segments)
  ├── merge_trees.py ──→ concept_matcher.py, xbrl_tree.py
  ├── pymodel.py ──→ xbrl_tree.py, llm_invariant_fixer.py
  │     └── llm_invariant_fixer.py ──→ llm_utils.py
  └── sheets/ (api, renderers, formatting, layouts, formulas)
        └── gws_utils.py

web/app.py
  ├── run_pipeline.py
  └── lookup_company.py
```

---

## How to Use This Map

- **Want to run the pipeline?** → `python run_pipeline.py AAPL`
- **Looking for XBRL parsing logic?** → `xbrl/` package (modern) or `parse_xbrl_facts.py` (legacy)
- **Debugging a verification failure?** → `pymodel.py` (checks) + `llm_invariant_fixer.py` (auto-fix)
- **Want to understand sheet generation?** → `sheets/` package
- **Need to add a new SEC fetcher?** → `fetch_10k.py` / `fetch_20f.py` pattern
- **Looking for tests?** → `tests/` directory
- **One-off analysis?** → `scripts/` directory
