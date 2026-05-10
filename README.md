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

| Stage | Module | What it does | LLM? |
|-------|--------|--------------|------|
| **1. Fetch Filings** | `fetch/agent.py` | Resolves ticker → CIK via SEC EDGAR, fetches filing URLs | Managed Agent |
| **2. Build Trees** | `xbrl/` package (CLI: `xbrl/cli.py`) | Parses iXBRL tags + calculation linkbase → tree with values | No |
| **3. Verify Invariants** | `model/verify.py` | Checks 5 cross-statement links against the parsed trees | No |
| **4. Write Google Sheet** | `sheets/` package (CLI: `sheets/builder.py`) | Renders trees into a multi-tab Google Sheet with `=SUM()` and cross-sheet formulas | No |
| **Orchestrator** | `run_pipeline.py` | Runs all stages sequentially with completeness gates | — |

> **IMPORTANT:** Always use `run_pipeline.py` to generate sheets. Running individual scripts bypasses the tree completeness gate and will produce sheets with broken formulas.

### Debugging Commands

```bash
# Inspect tree structure
python -m xbrl.cli --url <filing_url> -o trees.json

# Check invariants without writing sheet
python -m model.verify --trees trees.json --checkpoint
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

Top-level layout:

```
run_pipeline.py        — main orchestrator (only script at root)
fetch/                 — SEC discovery + HTTP
  agent.py             — Stage 1 ticker → filings (was agent1_fetcher.py)
  lookup.py            — ticker/name → CIK (was lookup_company.py)
  ten_k.py             — 10-K filing list (was fetch_10k.py)
  twenty_f.py          — 20-F filing list (was fetch_20f.py)
  http.py              — SEC-compliant fetch with rate limiting (was sec_utils.py)
xbrl/                  — Stage 2: XBRL parsing engine
  __init__.py          — package exports + build_statement_trees(), reconcile_trees()
  tree.py, linkbase.py, reconcile.py, segments.py
  facts_legacy.py      — iXBRL fact mapper for non-calc-linkbase filings (was parse_xbrl_facts.py)
  cli.py               — `python -m xbrl.cli ...` (was xbrl_tree.py)
merge/                 — Stage 3: cross-filing merge
  trees.py             — merge_filing_trees()           (was merge_trees.py)
  concepts.py          — concept matcher                (was concept_matcher.py)
model/                 — Stage 4: invariants + LLM repair
  verify.py            — `verify_model()`, run_checkpoint()  (was pymodel.py)
  llm_fixer.py         — LLM-in-loop reconciliation     (was llm_invariant_fixer.py)
sheets/                — Stage 5: Google Sheets renderer
  __init__.py          — write_sheets()
  api.py, renderers.py, formatting.py, layouts.py, formulas.py
  builder.py           — `python -m sheets.builder ...` (was sheet_builder.py)
  gws.py               — gws CLI wrappers                (was gws_utils.py)
llm/                   — LLM client (OpenAI-compatible)
  client.py            — call_llm(), get_llm_client()    (was llm_utils.py)
web/                   — FastAPI demo
tests/                 — test suite
scripts/               — dev tooling
```

### Analysis & Debugging
*   `scripts/compare_views.py`: Compares calc and presentation linkbase views for a filing.
*   `scripts/test_alignment.py`: Validates alignment between calc linkbase structure and iXBRL facts.
*   `scripts/test_cascade.py`: Tests cascade layout rendering for income statement trees.
*   `scripts/download_test_fixtures.py`: Downloads test fixture data for local testing.

### Test Suite
*   `tests/test_dual_linkbase.py`: Dual linkbase parsing unit tests.
*   `tests/test_merge_layers.py`: Three-layer merge tests (synthetic + 10 real companies).
*   `tests/test_merge_pipeline.py`: Multi-tree merge pipeline tests.
*   `tests/test_offline_e2e.py`: Offline end-to-end pipeline tests.
*   `tests/test_sheet_formulas.py`: Google Sheets formula generation tests.
*   `tests/test_da_sbc_tagging.py`: D&A and SBC tag identification tests.
*   `tests/test_model_historical.py`: Historical model computation tests.
*   `tests/test_model_historical_legacy.py`: Historical model tests for legacy (non-XBRL) filings.

### Web Demo
*   `web/app.py`: FastAPI backend — serves static UI, proxies search + pipeline jobs.
*   `web/static/index.html`: Single-page vanilla JS frontend (search → run → done).

## Setup & Requirements

- **Python 3.10+**
- **LLM API Key** (`LLM_API_KEY`): An OpenAI-compatible endpoint, used only for LLM-in-the-loop semantic reconciliation (`llm_invariant_fixer.py`). Defaults to Groq (`LLM_BASE_URL`, `LLM_MODEL`). Skip if you don't need invariant repair.
- **`gws` CLI**: Required for exporting models to Google Sheets (must be pre-authenticated via OAuth).
- **Podman**: Recommended for containerized execution (project preference over Docker).
- **FastAPI + Uvicorn**: Required for the demo website (`pip install fastapi uvicorn[standard]`).

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
python tests/test_merge_layers.py
```

## Demo Website

A local-only browser UI wrapping the full pipeline. Single user, one job at a time.

```bash
cd sec-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SEC_CONTACT_EMAIL="you@example.com"   # optional — falls back to demo@example.com
gws auth login                                # ensure Sheets OAuth is fresh
uvicorn web.app:app --reload
# then open http://localhost:8000
```

**Flow:** Type a ticker → select from matches → pipeline runs → Google Sheet link appears.

**Pre-flight checklist:**
- Dependencies installed via `pip install -r requirements.txt` (bs4, anthropic, fastapi, uvicorn, etc.)
- `SEC_CONTACT_EMAIL` env var set — recommended for SEC EDGAR compliance, but not required (fallback `demo@example.com` is used if unset)
- `gws` CLI authenticated and token not expired (otherwise sheet generation fails inside `sheets.write_sheets`)
- Run from `sec-agent/` directory (`outdir="./pipeline_output"` is cwd-relative)

## Deploy on a VPS (podman + Cloudflare Tunnel)

The repo ships a two-container stack: the FastAPI app and a Cloudflare Tunnel connector. The tunnel makes an outbound connection to Cloudflare's edge, so:

- **No public IP needed** — works fine if the VPS IP changes.
- **No inbound ports open** — only outbound 443 to Cloudflare.
- **TLS is terminated by Cloudflare** — no Caddy / Let's Encrypt setup required.

### Prerequisites
- A VPS with `podman` and `podman-compose` (Debian/Ubuntu: `sudo apt install podman podman-compose`).
- A domain managed by Cloudflare DNS (free plan is fine).
- A Groq API key (free tier) from console.groq.com — or any other OpenAI-compatible endpoint.

### One-time Cloudflare setup

1. Open the **Cloudflare Zero Trust dashboard** → `https://one.dash.cloudflare.com/` (free plan).
2. **Networks → Tunnels → Create a tunnel** → connector type `Cloudflared` → name it `sec-agent`.
3. Copy the **tunnel token** (the long `eyJhIj...` string after `--token` on the install page). You won't run those install commands; the token goes into `.env`.
4. Click **Next** and add a Public Hostname:
   - Subdomain: `demo` (or any)
   - Domain: your domain
   - Type: `HTTP`
   - URL: `sec-agent:8000`  *(the container name, not localhost)*
5. **Save tunnel.** Cloudflare auto-creates the DNS CNAME for you.

### One-time VPS setup

```bash
# 1. Clone on the VPS
git clone <repo-url> sec-agent && cd sec-agent

# 2. Configure environment
cp .env.example .env
$EDITOR .env   # set CLOUDFLARE_TUNNEL_TOKEN, LLM_API_KEY, SEC_CONTACT_EMAIL

# 3. Build the image (step 4 needs it)
podman-compose build

# 4. Pre-authenticate gws (interactive, one-time)
mkdir -p gws-config
podman run --rm -it -v ./gws-config:/root/.config/gws:Z \
  sec-agent:latest gws auth login
# Open the printed URL in any browser, paste the code back.

# 5. Bring up the stack
podman-compose up -d
podman logs -f cloudflared    # confirm "Registered tunnel connection"
```

Visit `https://demo.your-domain.com/`. The tunnel connects in seconds and Cloudflare's edge cert is already valid — no waiting for Let's Encrypt.

### Auto-restart on reboot

```bash
podman generate systemd --new --files --name sec-agent
podman generate systemd --new --files --name caddy
mkdir -p ~/.config/systemd/user && mv container-*.service ~/.config/systemd/user/
systemctl --user enable --now container-sec-agent.service container-caddy.service
loginctl enable-linger $USER
```

### Switching LLM provider

Edit `.env`. Defaults are Groq; OpenAI / Together / Ollama all use the same OpenAI-compatible Chat Completions API — only `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` change. The LLM is invoked rarely (only when `verify_model()` finds an invariant mismatch), so cost is minimal.

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
