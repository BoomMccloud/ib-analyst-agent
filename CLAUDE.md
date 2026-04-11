# SEC Financial Modeling Pipeline

Multi-stage pipeline that fetches SEC filings, extracts financials via XBRL, builds a financial model, and outputs a Google Sheet.

## Pipeline Stages

Each stage runs independently via CLI. Output JSON from one stage is the input to the next.

| Stage | Script(s) | What it does | LLM? |
|-------|-----------|--------------|------|
| 1 | `agent1_fetcher.py` | Resolves ticker → CIK via SEC EDGAR, fetches filing URLs | Managed Agent |
| 2a | `extract_sections.py` | Downloads filing HTML, slices into section .txt files + iXBRL facts | No |
| 2b | `xbrl_tree.py` | Parses iXBRL tags + calculation linkbase → tree with values | No |
| 2c | `xbrl_group.py` | Tree → structured JSON. Optional LLM groups small siblings | Optional (Haiku) |
| 2b-legacy | `structure_financials.py` | LLM structures sections into JSON (fallback for non-XBRL) | Sonnet + Haiku |
| 3 | `agent3_modeler.py` | Builds bottom-up financial model spec | Sonnet |
| 4 | `pymodel.py` | Computes 3-statement model, writes to Google Sheets | No |

## Running the Pipeline

**IMPORTANT: Always use `run_pipeline.py` to generate sheets.** Running `xbrl_tree.py`, `pymodel.py`, and `sheet_builder.py` individually bypasses the tree completeness gate and will produce sheets with broken formulas. The pipeline gate checks that every parent's `=SUM(children)` matches its declared XBRL value before writing the sheet.

```bash
# Full pipeline (preferred — includes all gates):
python run_pipeline.py AAPL

# Individual scripts (for debugging ONLY, not for sheet generation):
python xbrl_tree.py --url <filing_url> -o trees.json      # inspect tree
python pymodel.py --trees trees.json --checkpoint          # check invariants
# Do NOT run sheet_builder.py directly — use run_pipeline.py
```

### Legacy paths (for reference)

```bash
# Stage 1: Get filing URLs
python agent1_fetcher.py AAPL --years 5 > filings.json

# Stage 2 (XBRL path — recommended):
python xbrl_group.py --url <filing_url> -o structured.json          # with LLM grouping
python xbrl_group.py --url <filing_url> --no-llm -o structured.json # fully deterministic
python xbrl_group.py --url <filing_url> --print                     # inspect tree

# Stage 2 (Legacy LLM path — fallback):
python extract_sections.py <filing_url> --output-dir ./sections
python structure_financials.py ./sections -o structured.json

# Checkpoint (verify invariants):
python pymodel.py --financials structured.json --checkpoint

# Stage 3: Build model spec
python agent3_modeler.py --structured structured.json --company "Apple Inc." -o model.json

# Stage 4: Compute model + write Google Sheet
python pymodel.py --financials structured.json --company "Apple Inc."
```

## XBRL-Based Extraction (Phase 1b)

The XBRL path (`xbrl_tree.py` + `xbrl_group.py`) replaces the LLM-based extraction for financial statements. It parses:

1. **iXBRL tags** (`<ix:nonFraction>`) — every number in the filing, with exact values
2. **Calculation linkbase** (`_cal.xml`) — parent/child relationships with weights (+1/-1)
3. **Contexts** (`<xbrli:context>`) — period dates and entity segments

Key design decisions:
- **Position-based extraction**: BS_TA = Assets tree root, BS_TL = first L&E child, BS_TE = last L&E child. No name matching.
- **Cross-statement reconciliation**: INC_NET comes from CF's ProfitLoss leaf (authoritative). BS_CASH = CF_ENDC by construction.
- **Siblings can group, parent-child can't**: The LLM only groups additive siblings under the same parent. It never crosses subtraction boundaries.
- **Complete period filtering**: Only periods with data in ALL statement trees (IS + BS Assets + BS L&E + CF) are included.

Tested on 10 companies across 6 industries: 9/10 ALL PASS, 1 has a $401 rounding error.

See `docs/impl_guide_phase1b.md` for full details.

## Tautological API (Phase 1)

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

## Utility Scripts

- `lookup_company.py` — Resolves ticker/name → CIK, determines domestic (10-K) vs foreign (20-F)
- `fetch_10k.py` / `fetch_20f.py` — Fetches filing metadata from SEC EDGAR submissions API
- `parse_xbrl_facts.py` — Standalone XBRL tag → model code mapper (Phase 1b prototype)
- `sec_filings_agent.py` — Standalone Managed Agent for ad-hoc filing lookups
- `run_agent.py` — Generic: embeds any .py file and runs it in a Managed Agent
- `run_and_verify.py` — QA tool: runs `fetch_10k.py` locally then has a Managed Agent independently verify results
- `test_phase1_e2e.sh` — End-to-end test script for any ticker

## External Dependencies

- **Anthropic API** (`ANTHROPIC_API_KEY`) — Managed Agents (beta) for Agent 1; optional Haiku for sibling grouping in Stage 2c; Sonnet for model spec in Stage 3
- **SEC EDGAR** — company_tickers.json, submissions API, filing archives, iXBRL linkbases. Rate-limited to 8 req/s with backoff
- **`gws` CLI** — Google Workspace CLI for Sheets API (must be pre-authenticated via OAuth)
- **Models**: `claude-sonnet-4-6` for precision tasks, `claude-haiku-4-5-20251001` for grouping/large-text

## Architecture Notes

- **Deterministic-first**: XBRL parsing, CIK resolution, and file downloads are pure Python stdlib. LLMs only handle tasks requiring judgment (sibling grouping, model specs).
- **Position over names**: Financial statement structure identified by tree position, not concept name matching. Works across all industries.
- **No orchestration layer**: The pipeline is a manual convention — each script writes JSON that the next reads via CLI args. Each stage can be re-run independently.
- **Two extraction paths**: XBRL (deterministic, 9/10 companies) and LLM legacy (fallback for non-XBRL filings).

## Use podman, not docker.
