# SEC Financial Modeling Pipeline

4-stage pipeline that fetches SEC filings, extracts financials, builds a model spec, and outputs a Google Sheet.

## Pipeline Stages

Each stage runs independently via CLI. Output JSON from one stage is the input to the next.

| Stage | Agent | Script(s) | What it does |
|-------|-------|-----------|--------------|
| 1 | Ying (Fetcher) | `agent1_fetcher.py` | Resolves ticker → CIK via SEC EDGAR, fetches filing URLs. Uses Anthropic Managed Agent (cloud env with net access) to run `lookup_company.py` + `fetch_10k.py`/`fetch_20f.py` |
| 2a | Michelle (Extractor) | `extract_sections.py` | Downloads filing HTML, slices by TOC anchors into section .txt files + manifest.json. Pure stdlib, no LLM |
| 2b | Michelle (Extractor) | `structure_financials.py` | LLM structures each section into JSON. Sonnet for financials (IS/BS/CF), Haiku for notes/MD&A |
| 3 | Jason (Modeler) | `agent3_modeler.py` | Single Sonnet call producing a bottom-up financial model spec (revenue drivers, expense model, cash flow model) |
| 4 | Laura (Spreadsheet) | `agent4_spreadsheet.py` or `build_model_sheet.py` | Writes to Google Sheets via `gws` CLI |

## Running the Pipeline

```bash
# Stage 1: Get filing URLs
python agent1_fetcher.py AAPL --years 5 > filings.json

# Stage 2a: Extract sections from a filing URL
python extract_sections.py <filing_url> --output-dir ./sections

# Stage 2b: Structure extracted sections into JSON
python structure_financials.py ./sections -o structured.json

# Stage 3: Build model spec
python agent3_modeler.py --structured structured.json --company "Apple Inc." -o model.json

# Stage 4: Create Google Sheet (two options)
python agent4_spreadsheet.py --model model.json --financials structured.json --company "Apple Inc."
# OR (more robust, builds from scratch):
python build_model_sheet.py --model model.json --financials structured.json --company "Apple Inc."
```

## Two Spreadsheet Implementations

- **`agent4_spreadsheet.py`** — Template-based. Copies a template sheet (`template_row_map.json` defines row layout), fills hardcoded rows. Simpler but brittle across different companies.
- **`build_model_sheet.py`** — Code-driven. Assigns short codes to each line item (e.g., `REVT`, `GP`, `BS_CASH`), populates a `Filing` sheet, then IS/BS/CF sheets pull via `SUMIF`. More robust and maintainable.

## Utility Scripts

- `lookup_company.py` — Resolves ticker/name → CIK, determines domestic (10-K) vs foreign (20-F)
- `fetch_10k.py` / `fetch_20f.py` — Fetches filing metadata from SEC EDGAR submissions API
- `create_template.py` — One-time setup: reads an existing model spreadsheet, classifies rows as INPUT/FORMULA, clears inputs, saves `template_row_map.json`
- `sec_filings_agent.py` — Standalone Managed Agent for ad-hoc filing lookups (no pre-written scripts)
- `run_agent.py` — Generic: embeds any .py file and runs it in a Managed Agent
- `run_and_verify.py` — QA tool: runs `fetch_10k.py` locally then has a Managed Agent independently verify results

## External Dependencies

- **Anthropic API** (`ANTHROPIC_API_KEY`) — Managed Agents (beta) for Agent 1; direct `messages.create` for Agents 2b and 3
- **SEC EDGAR** — company_tickers.json, submissions API, filing archives. Rate-limited to 8 req/s with backoff
- **`gws` CLI** — Google Workspace CLI for Sheets API (must be pre-authenticated via OAuth)
- **Models**: `claude-sonnet-4-6` for precision tasks, `claude-haiku-4-5-20251001` for large-text/lower-precision

## Architecture Notes

- **Deterministic-first**: HTML parsing, CIK resolution, and file downloads are pure Python stdlib. LLMs only handle tasks requiring judgment (structuring financials, building model specs).
- **No orchestration layer**: The pipeline is a manual convention — each script writes JSON that the next reads via CLI args. Each stage can be re-run independently.
- **Defensive JSON parsing**: All LLM output extraction uses truncation recovery (closing unmatched braces), code-fence stripping, and regex fallback.
- **Managed Agents are used only where cloud networking is required** (Agent 1). All other stages run locally.

## Use podman, not docker.
