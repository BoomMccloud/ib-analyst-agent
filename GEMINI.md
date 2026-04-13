# SEC Financial Modeling Pipeline - Gemini Context

This project is an automated, tree-based pipeline designed to automate the creation of financial models from SEC filings (10-K, 20-F). It uses a deterministic, XBRL tree-based parsing approach, minimizing reliance on LLMs.

## Core Philosophy

- **Python-First Computation**: All financial logic, modeling, and invariant checks happen in Python (`pymodel.py`). Google Sheets is strictly a display layer.
- **Deterministic-First**: XBRL parsing, linkbase merging, CIK resolution, and file downloads are pure Python standard library operations. LLMs are reserved for tasks requiring judgment (e.g., semantic reconciliation of invariant failures, fallback for non-XBRL filings, sibling grouping).
- **Three-Layer Merge Principle**: The system builds on a three-layer merge of XBRL linkbases: Calc layer (mathematical truth), Presentation layer (display order), and an "Other" layer (gap absorption).
- **Validation-Centric**: No model is output to a spreadsheet until all accounting invariants (e.g., Assets = Liabilities + Equity) are verified against the parsed trees.
- **Position over Names**: Financial statement structure is identified by tree position (e.g., BS_TA = Assets tree root), not by concept name matching.

## Pipeline Architecture

| Stage | Focus | Primary Script | Input | Output |
|-------|-------|----------------|-------|--------|
| **1** | Fetcher | `agent1_fetcher.py` | Ticker | SEC filing URLs |
| **2** | Builder | `xbrl_tree.py` | URLs / iXBRL | `trees.json` |
| **3** | Verifier | `pymodel.py` | `trees.json` | Invariant checks |
| **4** | Sheet | `sheet_builder.py` | `trees.json` | Final Google Sheet |
| **All** | Orchestrator| `run_pipeline.py` | Ticker | Final Google Sheet |

## Key Components & Utilities

- **`xbrl_tree.py`**: The deterministic extraction engine that parses iXBRL facts and linkbases.
- **`pymodel.py`**: The verifier engine that enforces cross-statement checks and accounting invariants.
- **`sheet_builder.py`**: Converts the validated trees into a 4-tab Google Sheet using `=SUM()` and cross-sheet formulas.
- **`run_pipeline.py`**: Orchestrates the full pipeline with completeness gates.
- **`sec_utils.py`**: SEC EDGAR compliant fetching with built-in rate-limiting (0.15s interval / 8 req/s max) and caching.
- **`gws_utils.py`**: Wrappers for the `gws` CLI to interact with Google Sheets.
- **`llm_utils.py`**: Shared logic for calling Anthropic models when necessary.

## Technical Standards

- **Models**: Use `claude-sonnet-4-6` for precision tasks and `claude-haiku-4-5-20251001` for high-volume grouping or text tasks.
- **Environment**: Use **Podman** for containerization (never Docker).
- **SEC Compliance**: Respect the SEC rate limit; use the `fetch_url` helper or built-in rate limiters.
- **Naming**: Use snake_case for Python identifiers and Title Case for spreadsheet labels.
- **Invariants**: Every model must pass the `verify_model()` checks in `pymodel.py` before final delivery.

## Common Workflows

```bash
# Full Pipeline Execution Example (Apple)
python run_pipeline.py AAPL

# Inspect tree structure
python xbrl_tree.py --url <filing_url> -o trees.json

# Check invariants without writing sheet
python pymodel.py --trees trees.json --checkpoint
```

## Maintenance Notes

- **Always use `run_pipeline.py`** to generate sheets. Running individual scripts bypasses the tree completeness gate and will produce sheets with broken formulas.
- If a company uses unique financial terminology, rely on the XBRL structure and grouping logic rather than hardcoded names.
- Always verify `gws` authentication before running Stage 4.
fy `gws` authentication before running Stage 4.
