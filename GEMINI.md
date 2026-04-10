# SEC Financial Modeling Pipeline - Gemini Context

This project is a 4-stage pipeline designed to automate the creation of financial models from SEC filings (10-K, 20-F).

## Core Philosophy

- **Python-First Computation**: All financial logic, modeling, and invariant checks happen in Python (`pymodel.py`). Google Sheets is strictly a display layer.
- **Deterministic-First**: HTML parsing, CIK resolution, and file downloads use standard Python libraries. LLMs are reserved for tasks requiring judgment (structuring financials, classifying line items).
- **Defensive LLM Integration**: Output parsing includes truncation recovery, code-fence stripping, and regex fallbacks (`llm_utils.py`).
- **Validation-Centric**: No model is output to a spreadsheet until all accounting invariants (e.g., Assets = Liabilities + Equity) are verified.

## Pipeline Architecture

| Stage | Focus | Primary Script | Input | Output |
|-------|-------|----------------|-------|--------|
| **1** | Fetcher | `agent1_fetcher.py` | Ticker | `filings.json` (URLs) |
| **2a**| Slicer | `extract_sections.py` | URL | Section `.txt` files |
| **2b**| Structurer| `structure_financials.py`| Sections | `structured.json` |
| **3** | Modeler | `agent3_modeler.py` / `pymodel.py`| Structured JSON | `model.json` / GSheet |
| **4** | Sheet | `build_model_sheet.py` | Model JSON | Final Spreadsheet |

## Key Components & Utilities

- **`llm_utils.py`**: Shared logic for calling Anthropic models and parsing/repairing JSON responses.
- **`sec_utils.py`**: SEC EDGAR compliant fetching with built-in rate-limiting (0.15s interval).
- **`financial_utils.py`**: Standardized code definitions (`BS_CASH`, `REV1`, etc.) and flattening logic for financial statements.
- **`gws_utils.py`**: Wrappers for the `gws` CLI to interact with Google Sheets.
- **`pymodel.py`**: The "gold standard" modeling engine that computes 5-year forecasts and verifies invariants.

## Technical Standards

- **Models**: Use `claude-3-5-sonnet-20241022` for precision (Financials, Modeling) and `claude-3-5-haiku-20241022` for high-volume text (MD&A, Notes).
- **Environment**: Use **Podman** for containerization (never Docker).
- **SEC Compliance**: Respect the 10 req/s rate limit; use the `fetch_url` helper in `sec_utils.py`.
- **Naming**: Use snake_case for Python identifiers and Title Case for spreadsheet labels.
- **Invariants**: Every model must pass the `verify_model` checks in `pymodel.py` before final delivery.

## Common Workflows

```bash
# Full Pipeline Execution Example (Apple)
python agent1_fetcher.py AAPL --years 3 > filings.json
# (Repeat for each year in filings.json)
python extract_sections.py <url> --output-dir ./sections
python structure_financials.py ./sections -o structured_2024.json
# Final Modeling & Sheet Output
python pymodel.py --financials structured_2024.json structured_2023.json --company "Apple Inc."
```

## Maintenance Notes

- If the SEC HTML structure changes, update `extract_sections.py`.
- If a company uses unique financial terminology, update `financial_utils.py` or the LLM classification prompts in `structure_financials.py`.
- Always verify `gws` authentication before running Stage 4.
