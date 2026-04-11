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

| Stage | Script | Description |
|---|---|---|
| **1. Fetch Filings** | `agent1_fetcher.py` | Resolves ticker to CIK and retrieves filing metadata/URLs from SEC EDGAR using a managed agent. |
| **2. Build Trees** | `xbrl_tree.py` | Parses iXBRL and `_cal.xml`/`_pre.xml` into reconciled `IS`, `BS`, `BS_LE`, and `CF` trees, reordered by presentation. |
| **3. Verify Invariants**| `pymodel.py` | Checks 5 cross-statement links: BS Balance, Cash Link, NI Link, D&A Link, and SBC Link. |
| **4. Write Google Sheet** | `sheet_builder.py` | Renders the trees into a multi-tab Google Sheet using `gws` CLI, generating mathematically connected `=SUM()` and cross-sheet formulas. |
| **Orchestrator** | `run_pipeline.py` | Runs all stages sequentially. |

## File Reference

### Pipeline Scripts
*   `agent1_fetcher.py`: Entry point for Stage 1. Handles ticker resolution and filing discovery.
*   `xbrl_tree.py`: The deterministic extraction engine. Builds structural trees from iXBRL facts and linkbases.
*   `pymodel.py`: Verifier for cross-statement checks and accounting invariants.
*   `sheet_builder.py`: Converts trees to 4-tab Google Sheets (IS, BS, CF, Summary) via `gws` CLI.
*   `run_pipeline.py`: Orchestrates the full pipeline.

### Supporting Utilities
*   `lookup_company.py`: Maps tickers or company names to SEC Central Index Keys (CIK).
*   `fetch_10k.py` / `fetch_20f.py`: Targeted scripts for fetching filing metadata from the SEC API.
*   `parse_xbrl_facts.py`: iXBRL tag extraction from filing HTML.
*   `sec_utils.py`: SEC-compliant HTTP fetching with rate limiting (0.15s intervals) and caching.
*   `llm_utils.py`: Shared utilities for calling Anthropic models.
*   `gws_utils.py`: Helpers for interacting with Google Sheets via the `gws` command-line tool.

## Setup & Requirements

- **Python 3.10+**
- **Anthropic API Key**: Required for LLM-based filing discovery.
- **`gws` CLI**: Required for exporting models to Google Sheets.
- **Podman**: Recommended for containerized execution (project preference over Docker).

## Usage

### Run the Pipeline
Run the entire pipeline with a single command to generate a spreadsheet.
```bash
python run_pipeline.py AAPL
```

### Run the Tests
Tests are divided into tree parsing logic, verification logic, and end-to-end pipelines.
```bash
# All unit tests
python -m pytest tests/test_dual_linkbase.py -v

# Three-layer merge tests (synthetic + 10 real companies)
python test_merge_layers.py
```

For more detailed technical constraints and coding standards, see [GEMINI.md](./GEMINI.md).
