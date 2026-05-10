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

See `docs/impl_guide_phase1b.md` for full details.

## Tautological API (Phase 1)

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
- `xbrl/facts_legacy.py` — Standalone XBRL tag → model code mapper (Phase 1b prototype)
- `test_phase1_e2e.sh` — End-to-end test script for any ticker

## External Dependencies

- **LLM API** (`LLM_API_KEY`, OpenAI-compatible) — used by `model/llm_fixer.py` only when invariants fail. Defaults to Groq.
- **SEC EDGAR** — company_tickers.json, submissions API, filing archives, iXBRL linkbases. Rate-limited to 8 req/s with backoff
- **`gws` CLI** — Google Workspace CLI for Sheets API (must be pre-authenticated via OAuth)
- **Models**: `claude-sonnet-4-6` for precision tasks, `claude-haiku-4-5-20251001` for grouping/large-text

## Architecture Notes

- **Deterministic-first**: XBRL parsing, CIK resolution, and file downloads are pure Python stdlib. LLMs only handle tasks requiring judgment (sibling grouping, model specs).
- **Position over names**: Financial statement structure identified by tree position, not concept name matching. Works across all industries.
- **No orchestration layer**: The pipeline is a manual convention — each script writes JSON that the next reads via CLI args. Each stage can be re-run independently.
- **Two extraction paths**: XBRL (deterministic, 9/10 companies) and LLM legacy (fallback for non-XBRL filings).

## Use podman, not docker.
 filings).

## Use podman, not docker.
