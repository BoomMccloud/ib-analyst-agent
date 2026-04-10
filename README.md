# SEC Financial Modeling Pipeline

An automated, 4-stage pipeline that transforms raw SEC EDGAR filings into functional, multi-year financial models in Google Sheets.

## Overview

This project automates the workflow of an investment banking analyst:
1.  **Discovery**: Finding and downloading the correct SEC filings (10-K, 20-F).
2.  **Extraction**: Slicing massive HTML filings into manageable sections (Income Statement, Balance Sheet, Cash Flows, MD&A).
3.  **Structuring**: Using LLMs (Claude) to convert unstructured financial tables into clean, standardized JSON data.
4.  **Modeling**: Computing forecasts and generating a 3-statement financial model that balances automatically.

## Pipeline Stages

| Stage | Name | Key Script | Description |
|---|---|---|---|
| **1** | **Fetcher** | `agent1_fetcher.py` | Resolves ticker to CIK and retrieves filing metadata/URLs from SEC EDGAR. |
| **2a** | **Slicer** | `extract_sections.py` | Parses HTML filings and splits them into section-specific text files based on TOC anchors. |
| **2b** | **Structurer**| `structure_financials.py` | Uses LLMs to structure financials into JSON and classify line items into standard codes. |
| **3** | **Modeler** | `pymodel.py` | Computes a 5-year forecast and verifies accounting invariants (e.g., Assets = Liabilities + Equity). |
| **4** | **Exporter** | `build_model_sheet.py` | Exports the verified model to Google Sheets via the `gws` CLI. |

## File Reference

### Core Pipeline
*   `agent1_fetcher.py`: Entry point for Stage 1. Handles ticker resolution and filing discovery.
*   `extract_sections.py`: Pure-Python HTML parser that extracts specific sections (e.g., Item 8) from filings.
*   `structure_financials.py`: The "Michelle" agent. Orchestrates LLM calls to turn text tables into JSON.
*   `agent3_modeler.py`: Produces a bottom-up financial model specification using LLMs.
*   `pymodel.py`: The primary modeling engine. Performs all calculations in Python to ensure invariants pass before export.
*   `build_model_sheet.py`: The recommended Stage 4 exporter. Builds a robust, code-driven Google Sheet.
*   `agent4_spreadsheet.py`: Alternative Stage 4 exporter that uses a pre-defined template (`template_row_map.json`).

### Utilities
*   `llm_utils.py`: Shared utilities for calling Anthropic models, including defensive JSON repair for truncated responses.
*   `sec_utils.py`: SEC-compliant fetching logic with mandatory rate-limiting (0.15s intervals).
*   `financial_utils.py`: Definitions for standardized financial codes (e.g., `BS_CASH`, `REVT`) and data flattening logic.
*   `gws_utils.py`: Helpers for interacting with Google Sheets via the `gws` command-line tool.
*   `lookup_company.py`: Logic for mapping tickers or company names to SEC Central Index Keys (CIK).
*   `fetch_10k.py` / `fetch_20f.py`: Targeted scripts for fetching domestic and foreign filing metadata.

### Development & QA
*   `diagnose_model.py`: Diagnostic utility for inspecting and debugging model outputs.
*   `run_and_verify.py`: Verification tool that compares local SEC fetch results with Managed Agent outputs.
*   `create_template.py`: Utility to generate the `template_row_map.json` from an existing spreadsheet.
*   `sec_filings_agent.py`: A standalone Managed Agent for ad-hoc SEC research and filing lookups.

## Setup & Requirements

- **Python 3.10+**
- **Anthropic API Key**: Required for LLM-based structuring and modeling.
- **`gws` CLI**: Required for exporting models to Google Sheets.
- **Podman**: Recommended for containerized execution (project preference over Docker).

## Usage

### The Agentic Way (Recommended)
Run the entire pipeline with a single command. Defaults to 5 years of history and 5 years of forecasts.
```bash
python run_pipeline.py AAPL
```

### The Manual Way (Stage-by-Stage)
```bash
# 1. Fetch filings
python agent1_fetcher.py AAPL --years 3 > filings.json

# 2. Extract and Structure (simplified example)
python extract_sections.py <filing_url> --output-dir ./sections
python structure_financials.py ./sections -o structured.json

# 3. Build and Export Model
python pymodel.py --financials structured.json --company "Apple Inc."
```

For more detailed technical constraints and coding standards, see [GEMINI.md](./GEMINI.md).
