# SEC Financial Modeling Pipeline

Multi-stage pipeline that fetches SEC filings, extracts financials via XBRL, builds a financial model, and outputs a Google Sheet.

## Pipeline Stages

Each stage runs independently via CLI. Output JSON from one stage is the input to the next.

| Stage | Module | What it does | LLM? |
|-------|--------|--------------|------|
| 1 | `fetch/agent.py` | Resolves ticker → CIK via SEC EDGAR, fetches filing URLs | No |
| 2 | `xbrl/` package (CLI: `xbrl/cli.py`) | Parses iXBRL tags + calculation linkbase → tree with values | No |
| 3 | `merge/trees.py` | Merges multiple filings into one tree with full historical periods | No |
| 4 | `model/verify.py` | Cross-statement invariant checks. Falls back to `model/llm_fixer.py` if errors. | Optional |
| 5 | `sheets/` package (CLI: `sheets/builder.py`) | Renders trees into a multi-tab Google Sheet | No |

## Running the Pipeline

**IMPORTANT: Always use `run_pipeline.py` to generate sheets.** Running `xbrl/cli.py`, `model/verify.py`, and `sheets/builder.py` individually bypasses the tree completeness gate and will produce sheets with broken formulas. The pipeline gate checks that every parent's `=SUM(children)` matches its declared XBRL value before writing the sheet.

```bash
# Full pipeline (preferred — includes all gates):
python run_pipeline.py AAPL

# Individual modules (for debugging ONLY, not for sheet generation):
python -m xbrl.cli --url <filing_url> -o trees.json      # inspect tree
python -m model.verify --trees trees.json --checkpoint    # check invariants
# Do NOT run sheets/builder.py directly — use run_pipeline.py
```

## XBRL-Based Extraction

The XBRL path (`xbrl/` package) replaces the LLM-based extraction for financial statements. It parses:

1. **iXBRL tags** (`<ix:nonFraction>`) — every number in the filing, with exact values
2. **Calculation linkbase** (`_cal.xml`) — parent/child relationships with weights (+1/-1)
3. **Contexts** (`<xbrli:context>`) — period dates and entity segments

Key design decisions:
- **Position-based extraction**: BS_TA = Assets tree root, BS_TL = first L&E child, BS_TE = last L&E child. No name matching.
- **Cross-statement reconciliation**: INC_NET comes from CF's ProfitLoss leaf (authoritative). BS_CASH = CF_ENDC by construction.
- **Siblings can group, parent-child can't**: The LLM only groups additive siblings under the same parent. It never crosses subtraction boundaries.
- **Complete period filtering**: Only periods with data in ALL statement trees (IS + BS Assets + BS L&E + CF) are included.

Tested on 10 companies across 6 industries: 9/10 ALL PASS, 1 has a $401 rounding error.

### Revenue Segmentation & Decomposition
- **Filing Pagination & De-duplication**: Supports scanning SEC's paginated historical submissions (e.g. `submissions-001.json`) and de-duplicates by accession number in `ten_k.py` and `twenty_f.py` to ensure a clean 5-year retrieval for high-volume issuers.
- **Relaxed Sum (Gap-Aware) Matching**: Allows segment decompositions covering between 80% and 105% of the total revenue, absorbing the difference into a `Corporate & Other (Residual)` segment.
- **Prioritized Axis Selection**: Scans Product (`srt:ProductOrServiceAxis`), Business (`us-gaap:StatementBusinessSegmentsAxis`), and Geographic (`srt:StatementGeographicalAxis`) dimensions. Axis selection order is:
  1. 2-Level Nested Decompositions (e.g. Business by Product)
  2. Product & Service Axis
  3. Business Segments Axis
  4. Geographical Axis
- **Spreadsheet Integration**: Segment hierarchies are output as dynamic `TreeNode` structures. The spreadsheet builder renders them as parent-child nodes using Excel-style `=SUM` formulas linked directly to the main income statement.

## Tautological API

`model/verify.py` exposes enforce-by-construction helpers:
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

## Utility modules

- `fetch/lookup.py` — Resolves ticker/name → CIK, determines domestic (10-K) vs foreign (20-F)
- `fetch/ten_k.py` / `fetch/twenty_f.py` — Fetches filing metadata from SEC EDGAR submissions API
- `fetch/http.py` — Shared SEC EDGAR fetching, rate limiting, and compliance logic
- `llm/client.py` — OpenAI-compatible Chat Completions wrapper (Groq default)
- `model/llm_fixer.py` — LLM-in-the-loop semantic reconciliation for fixing cross-statement invariants
- `xbrl/facts.py` — iXBRL tag parser. Extracts every `<ix:nonFraction>` value with its period/context. Called by `build_statement_trees()` to build the facts dict that the calc-linkbase tree is hydrated against.

## External Dependencies

- **LLM API** (`LLM_API_KEY`, OpenAI-compatible) — used by `model/llm_fixer.py` only when invariants fail. Defaults to Groq.
- **SEC EDGAR** — company_tickers.json, submissions API, filing archives, iXBRL linkbases. Rate-limited to 8 req/s with backoff
- **`gws` CLI** — Google Workspace CLI for Sheets API (must be pre-authenticated via OAuth)
- **Models**: `llama-3.1-70b-versatile` (default via Groq). Configurable via `LLM_MODEL` env var.

## Architecture Notes

- **Deterministic-first**: XBRL parsing, CIK resolution, and file downloads are pure Python stdlib. LLMs only handle tasks requiring judgment (semantic invariant repair).
- **Position over names**: Financial statement structure identified by tree position, not concept name matching. Works across all industries.
- **Three-layer merge**: Trees are built from three XBRL linkbases — Calc (mathematical truth: parent = Σ children with signed weights), Presentation (display order), and an "Other" gap-absorption layer for facts not covered by calc relationships.
- **Validation-centric**: No model is written to a sheet until `verify_model()` passes. Mathematical invariants halt the pipeline; semantic mismatches are routed to `model/llm_fixer.py`.
- **No orchestration layer**: The pipeline is a manual convention — each script writes JSON that the next reads via CLI args. Each stage can be re-run independently.

## Use podman, not docker.
