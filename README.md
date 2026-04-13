# SEC Financial Modeling Pipeline

An automated, tree-based pipeline that transforms raw SEC EDGAR iXBRL filings into fully-linked, formula-driven Google Sheets financial models.

## Overview

This project automates the workflow of an investment banking analyst by building deterministic, mathematical trees from XBRL data. It uses a pure Python approach for extraction and modeling, falling back to LLMs only for tasks requiring judgment.

1.  **Discovery**: Finding and downloading the correct SEC filings (10-K, 20-F).
2.  **Tree Construction**: Parsing iXBRL facts and calculation/presentation linkbases into a reconciled parent-child mathematical tree structure.
3.  **Verification**: Running deterministic cross-statement invariants (e.g., Assets = Liabilities + Equity, Cash Flow Ending Cash = Balance Sheet Cash) against the parsed trees.
4.  **Sheet Rendering**: Generating a 3-statement Google Sheet with exact formulas and cross-statement references that balance by construction.

## Core Principle: Three-Layer Merge

The sheet rendering is built on a three-layer merge of XBRL linkbases:

1. **Calc layer** (mathematical truth): Parent-child tree with signed weights (+1/-1). Defines `=SUM(children * weight)` formulas.
2. **Presentation layer** (display order): Sibling ordering matching the 10-K layout (e.g., Revenue first, Net Income last).
3. **"Other" layer** (gap absorption): For any parent where `SUM(children) != declared_value`, an "Other" row absorbs the residual, guaranteeing every formula equals its declared XBRL value.

## Pipeline Stages

Each stage runs independently via CLI. Output JSON from one stage is the input to the next.

| Stage | Script(s) | What it does | LLM? |
|-------|-----------|--------------|------|
| **1. Fetch Filings** | `agent1_fetcher.py` | Resolves ticker → CIK via SEC EDGAR, fetches filing URLs | Managed Agent |
| **2. Build Trees** | `xbrl_tree.py` | Parses iXBRL tags + calculation linkbase → tree with values | No |
| **3. Verify Invariants** | `pymodel.py` | Checks 5 cross-statement links against the parsed trees | No |
| **4. Write Google Sheet** | `sheet_builder.py` | Renders trees into a multi-tab Google Sheet with `=SUM()` and cross-sheet formulas | No |
| **Orchestrator** | `run_pipeline.py` | Runs all stages sequentially with completeness gates | — |

> **IMPORTANT:** Always use `run_pipeline.py` to generate sheets. Running individual scripts bypasses the tree completeness gate and will produce sheets with broken formulas.

### Debugging Commands

```bash
# Inspect tree structure
python xbrl_tree.py --url <filing_url> -o trees.json

# Check invariants without writing sheet
python pymodel.py --trees trees.json --checkpoint
```

## XBRL-Based Extraction

The pipeline parses three layers of XBRL data:

1. **iXBRL tags** (`<ix:nonFraction>`) — every number in the filing, with exact values
2. **Calculation linkbase** (`_cal.xml`) — parent/child relationships with weights (+1/-1)
3. **Contexts** (`<xbrli:context>`) — period dates and entity segments

Key design decisions:
- **Position-based extraction**: BS_TA = Assets tree root, BS_TL = first L&E child, BS_TE = last L&E child. No name matching.
- **Cross-statement reconciliation**: INC_NET comes from CF's ProfitLoss leaf (authoritative). BS_CASH = CF_ENDC by construction.
- **Complete period filtering**: Only periods with data in ALL statement trees (IS + BS Assets + BS L&E + CF) are included.

Tested on 10 companies across 6 industries: 9/10 ALL PASS, 1 has a $401 rounding error.

## Tautological API

`pymodel.py` exposes enforce-by-construction helpers:
- `set_category()` — catch-all = subtotal - sum(flex), always
- `set_is_cascade()` — GP, OPINC, EBT, INC_NET computed from inputs
- `set_bs_totals()` — TA = TCA + TNCA, TL = TCL + TNCL
- `set_cf_totals()` — NETCH = OPCF + INVCF + FINCF + FX
- `set_cf_cash()` — ENDC = BEGC + NETCH

`verify_model()` checks 5 real invariants that can't be enforced by construction:
1. BS_TA == BS_TL + BS_TE
2. CF_ENDC == BS_CASH
3. INC_NET (IS) == INC_NET (CF) — value-matched, not position-hardcoded
4. D&A (IS) == D&A (CF)
5. SBC (IS) == SBC (CF)

## File Reference

### Pipeline Scripts
*   `agent1_fetcher.py`: Entry point for Stage 1. Handles ticker resolution and filing discovery.
*   `xbrl_tree.py`: The deterministic extraction engine. Builds structural trees from iXBRL facts and linkbases.
*   `pymodel.py`: Verifier for cross-statement checks and accounting invariants.
*   `sheet_builder.py`: Converts trees to 4-tab Google Sheets (IS, BS, CF, Summary) via `gws` CLI.
*   `run_pipeline.py`: Orchestrates the full pipeline with completeness gates.
*   `merge_trees.py`: Merges multiple filing trees into one with full historical data across periods.

### Supporting Utilities
*   `lookup_company.py`: Maps tickers or company names to SEC Central Index Keys (CIK).
*   `fetch_10k.py` / `fetch_20f.py`: Targeted scripts for fetching filing metadata from the SEC API.
*   `parse_xbrl_facts.py`: iXBRL tag extraction from filing HTML.
*   `sec_utils.py`: SEC-compliant HTTP fetching with rate limiting (0.15s intervals) and caching.
*   `llm_utils.py`: Shared utilities for calling Anthropic models.
*   `gws_utils.py`: Helpers for interacting with Google Sheets via the `gws` command-line tool.

### Analysis & Debugging
*   `compare_views.py`: Compares calc and presentation linkbase views for a filing.
*   `test_alignment.py`: Validates alignment between calc linkbase structure and iXBRL facts.
*   `test_cascade.py`: Tests cascade layout rendering for income statement trees.
*   `test_merge_layers.py`: Three-layer merge tests (synthetic + 10 real companies).
*   `scripts/download_test_fixtures.py`: Downloads test fixture data for local testing.

### Test Suite
*   `tests/test_dual_linkbase.py`: Dual linkbase parsing unit tests.
*   `tests/test_merge_pipeline.py`: Multi-tree merge pipeline tests.
*   `tests/test_offline_e2e.py`: Offline end-to-end pipeline tests.
*   `tests/test_sheet_formulas.py`: Google Sheets formula generation tests.
*   `tests/test_da_sbc_tagging.py`: D&A and SBC tag identification tests.
*   `tests/test_model_historical.py`: Historical model computation tests.
*   `tests/test_model_historical_legacy.py`: Historical model tests for legacy (non-XBRL) filings.

## Setup & Requirements

- **Python 3.10+**
- **Anthropic API Key** (`ANTHROPIC_API_KEY`): Required for LLM-in-the-loop semantic reconciliation (`llm_invariant_fixer.py`).
- **`gws` CLI**: Required for exporting models to Google Sheets (must be pre-authenticated via OAuth).
- **Podman**: Recommended for containerized execution (project preference over Docker).

### External Dependencies

- **SEC EDGAR**: company_tickers.json, submissions API, filing archives, iXBRL linkbases. Rate-limited to 8 req/s with backoff.
- **Models**: `claude-sonnet-4-6` for precision tasks, `claude-haiku-4-5-20251001` for grouping/large-text.

## Usage

### Run the Pipeline

Run the entire pipeline with a single command to generate a spreadsheet.

```bash
python run_pipeline.py AAPL
```

### Run the Tests

```bash
# All unit tests
python -m pytest tests/ -v

# Three-layer merge tests (synthetic + 10 real companies)
python test_merge_layers.py
```

## Architecture Notes

- **Deterministic-first**: XBRL parsing, CIK resolution, and file downloads are pure Python stdlib. LLMs only handle tasks requiring judgment (sibling grouping, model specs).
- **Position over names**: Financial statement structure identified by tree position, not concept name matching. Works across all industries.
- **No orchestration layer**: The pipeline is a manual convention — each script writes JSON that the next reads via CLI args. Each stage can be re-run independently.
- **Two extraction paths**: XBRL (deterministic, 9/10 companies) and LLM legacy (fallback for non-XBRL filings).

## Documentation

- [XBRL Linkbases](docs/xbrl_linkbases.md) — Deep dive into calc and presentation linkbase parsing
- [Pipeline Phase 3F](docs/pipeline_phase3f_combined_presentation_calc.md) — Combined presentation + calc merge design
- [Backlog](docs/backlog.md) — Project backlog and roadmap
- [CLAUDE.md](CLAUDE.md) — Developer context: detailed pipeline stages, legacy paths, and architecture notes
- [GEMINI.md](GEMINI.md) — Technical constraints and coding standards
 [GEMINI.md](GEMINI.md) — Technical constraints and coding standards
