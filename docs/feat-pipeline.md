# SEC Pipeline: Tree-Based 3-Statement Model Specification

## 1. Architecture Overview

The pipeline converts SEC 10-K / 20-F filings into a fully-linked Google Sheet
with formula-based financial statements. It is **deterministic-first**: ticker
lookup, filing fetch, XBRL parsing, tree merging, and sheet rendering are pure
Python. The LLM is only invoked as a *fallback repair step* if soft
cross-statement invariants fail after deterministic reconciliation
(see §5).

The full flow is orchestrated by `run_pipeline.py` and runs in five stages,
each producing JSON that the next stage consumes.

```
ticker ──▶ Stage 1: fetch ──▶ filings[]
              │
              ├──▶ Stage 2: build trees (per filing)  ──▶ trees_<date>.json
              │                                              │
              ├──▶ Stage 3: merge across filings  ◀──────────┘
              │             ──▶ merged.json
              │
              ├──▶ Stage 4: verify invariants (+ optional LLM fix)
              │
              └──▶ Stage 5: write Google Sheet  ──▶ {sheet_id, sheet_url}
```

### Core Principle: Three-Layer Merge

Each per-filing tree is built from a three-layer merge of XBRL linkbases:

1. **Calc layer** (mathematical truth): parent–child tree with signed weights
   (+1 / −1). Defines `=SUM(children * weight)` formulas. Source: `_cal.xml`.
2. **Presentation layer** (display order): sibling ordering matching the
   10-K layout (Revenue first, Net Income last). Source: `_pre.xml`.
3. **"Other" layer** (gap absorption): for any parent where
   `SUM(children) ≠ declared_value`, an `__OTHER__` residual row absorbs the
   gap. This guarantees every formula in the sheet equals its declared XBRL
   value by construction.

Cross-statement invariants (BS Balance, Cash Link, NI Link, …) therefore
hold in the sheet because every parent's formula reproduces the exact
XBRL-declared number.

### Cash Flow: Mixed Duration + Instant Facts

The CF statement uniquely combines two XBRL context types:
- **Duration facts** (flows): OPCF, INVCF, FINCF, FX, Net Change in Cash —
  these are in the calc tree.
- **Instant facts** (balances): Beginning Cash, Ending Cash — these are NOT
  in the calc tree.

The pipeline handles this by:
- Deriving the ending-cash concept from the CF root (strips the
  `PeriodIncreaseDecrease...` suffix to find the balance version), with
  fallbacks for variants like `...IncludingDisposalGroupAndDiscontinuedOperations`.
- Rendering Beginning Cash as a hard value (the prior period's ending balance).
- Rendering Ending Cash as a formula: `=Beginning Cash + Net Change`.
- Net Change is itself a calc-tree formula: `=OPCF + INVCF + FINCF + FX`.

---

## 2. File Layout

The codebase is organised as packages, not flat scripts. Each package
exposes a clean public surface used by `run_pipeline.py`.

### Pipeline driver

| File | Role | LLM? |
|------|------|------|
| `run_pipeline.py` | End-to-end orchestrator (`run_pipeline(query, years, outdir)`) | No |
| `config.py` | Env vars (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`) and `validate_environment()` | — |

### `fetch/` — SEC EDGAR access

| File | Role |
|------|------|
| `agent.py` | `run(query, years)` — entrypoint: lookup → filer info → fetch URLs |
| `lookup.py` | Ticker / name → CIK; determines domestic (10-K) vs foreign (20-F) |
| `ten_k.py` / `twenty_f.py` | Filing metadata via the SEC submissions API |
| `http.py` | Shared HTTP fetch with rate limiting and SEC compliance headers |

### `xbrl/` — iXBRL extraction & per-filing tree construction

Public entrypoint: `build_statement_trees(html, base_url)` in `xbrl/__init__.py`.

| File | Role |
|------|------|
| `facts.py` | iXBRL tag parser (`build_xbrl_facts_dict`, `build_segment_facts_dict`) |
| `linkbase.py` | Fetch + parse `_cal.xml`, `_pre.xml`, `_lab.xml` linkbases; classify statement roles |
| `tree.py` | `TreeNode`, `build_tree`, `build_presentation_index`, cascade layout helpers |
| `reconcile.py` | Position tagging, BS_CASH override, calc/pres merge, D&A/SBC tagging, `CROSS_STATEMENT_CHECKS` |
| `segments.py` | Revenue segment extraction and attachment via XBRL dimensions |
| `cli.py` | Standalone CLI: `python -m xbrl.cli --url <filing_url> -o trees.json` (debugging only) |

### `merge/` — Multi-filing consolidation

| File | Role |
|------|------|
| `trees.py` | `merge_filing_trees(tree_files)` — newest-first union into one tree |
| `concepts.py` | `ConceptMatcher` / `ConceptMap` — cross-filing concept alignment via value-matching |

### `model/` — Invariant verification

| File | Role |
|------|------|
| `verify.py` | `verify_model()`, `run_checkpoint()` → `CheckpointResult` |
| `llm_fixer.py` | Soft-invariant repair via LLM (move_role / change_weight ops) |

### `sheets/` — Google Sheets rendering

Public entrypoint: `write_sheets(trees, company)` in `sheets/__init__.py`.

| File | Role |
|------|------|
| `builder.py` | CLI shim (use `run_pipeline.py` instead — see "Running" below) |
| `renderers.py` | Per-tab body / segments / cash-proof / summary rendering |
| `formulas.py` | Column helpers (`dcol`), weight-aware `=SUM(...)` builders |
| `layouts.py` | Cascade vs. totals-at-bottom layouts |
| `formatting.py` | `_build_format_requests()` for fonts, borders, number formats |
| `api.py` | `gws_create()`, `gws_share()` (driver boundary for web/app) |
| `gws.py` | Subprocess wrapper around the `gws` CLI (`gws_write`, `gws_batch_update`) |

### `llm/` — OpenAI-compatible chat client

| File | Role |
|------|------|
| `client.py` | `get_llm_client()`, `call_llm()` — Chat Completions wrapper with JSON parsing and code-fence stripping |

`llm/client.py` defaults to **Groq** (`https://api.groq.com/openai/v1`) with
`llama-3.1-70b-versatile`. Override with `LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL`. It is consumed only by `model/llm_fixer.py`.

---

## 3. Running the Pipeline

**Always use `run_pipeline.py` to generate sheets.** Invoking
`xbrl/cli.py`, `model/verify.py`, or `sheets/builder.py` directly bypasses
the cross-statement reconciliation that `run_pipeline.py` enforces between
stages, and will produce sheets with broken formulas.

```bash
# Full pipeline (preferred)
python run_pipeline.py AAPL                 # default: 5 years
python run_pipeline.py "Alibaba" --years 3
python run_pipeline.py NFLX --outdir ./tmp  # custom intermediate dir

# Per-stage debugging only
python -m xbrl.cli --url <filing_url> -o trees.json
python -m model.verify --trees merged.json --checkpoint
python -m merge.trees trees_*.json -o merged.json
```

`run_pipeline()` is also importable for in-process use (e.g. from `web/app.py`):

```python
from run_pipeline import run_pipeline
result = run_pipeline("AAPL", years=5, on_progress=lambda stage, msg: ...)
# {"sheet_url": "...", "sheet_id": "...", "company_name": "Apple Inc."}
```

It raises `RuntimeError` on failure (no `sys.exit`) and writes intermediate
JSON to `outdir` (default `./pipeline_output/`): `trees_<date>.json` per
filing and `merged.json` for the consolidated tree.

---

## 4. Data Pipeline (per stage)

### Stage 1 — Fetch filings

**Entrypoint:** `fetch.agent.run(query, years)`
**Output:** dict with `company`, `ticker`, `cik`, `filer_type`,
`filing_type` (`10-K` or `20-F`), and `filings: [{filing_date, url, ...}]`.

Deterministic — no LLM. Resolves ticker or name to a CIK via
`fetch.lookup`, decides 10-K vs 20-F from `filer_info`, then queries the
SEC submissions API through `fetch.ten_k` or `fetch.twenty_f`.

### Stage 2 — Build per-filing reconciled tree

**Entrypoint:** `xbrl.build_statement_trees(html, base_url)`
**Output:** dict with `IS`, `BS`, `BS_LE`, `CF`, `revenue_segments`,
`facts`, `complete_periods`, `cf_endc_values`, `lab_labels`, `unit_label`.

For each filing URL, `run_pipeline.py` downloads the iXBRL HTML and calls
`build_statement_trees`, which performs:

1. **Parse iXBRL facts** (`xbrl.facts.build_xbrl_facts_dict`) — every
   `<ix:nonFraction>` tag with its period/context.
2. **Fetch + parse `_cal.xml`** (`xbrl.linkbase.parse_calc_linkbase`) —
   parent/child relationships with weights, grouped by role.
3. **Classify statement roles** (`classify_roles`) — IS / BS / CF.
4. **Pick the best root for each statement** — e.g. for IS prefer a root
   containing `NetIncomeLoss`; for BS prefer one ending in `Assets`.
5. **Hydrate trees** (`build_tree`) using the facts dict.
6. **Build presentation index** from `_pre.xml`
   (`parse_pre_linkbase` → `build_presentation_index`).
7. **Run `reconcile_trees()`** (see below).
8. **Attach segments** (`xbrl.segments`) using `_lab.xml` labels and
   dimensional segment facts; build `revenue_segments` tree if available.

`reconcile_trees()` performs the in-place reconciliation steps:

| Step | What it does |
|------|-------------|
| Tag BS positions | BS_TA, BS_TL, BS_TE, BS_TCA, BS_TCL, BS_CASH by tree position |
| Tag CF positions | CF_NETCH, CF_OPCF, CF_INVCF, CF_FINCF, CF_FX, INC_NET_CF, CF_BEGC by concept pattern |
| Tag IS positions | INC_NET by value-matching the CF authoritative NI; IS_REVENUE / IS_COGS by BFS keyword search |
| Tag IS semantic | D&A and SBC nodes via time-series value matching |
| Override BS_CASH | Replace BS_CASH values with CF_ENDC (cross-statement link) |
| Filter complete periods | Keep only periods present in IS + BS + BS_LE + CF |
| Three-layer merge | For each statement: reorder children by presentation order, insert `__OTHER__` rows for any parent whose children don't sum to its declared value |
| Tag D&A / SBC nodes | Cross-statement role tags `IS_DA` / `CF_DA`, `IS_SBC` / `CF_SBC` |

**Key design decisions:**
- **Position over names**: BS structure identified by tree position
  (root = TA, last L&E child = TE), not concept-name matching. Works
  across industries.
- **Presentation ordering via BeautifulSoup**: `_pre.xml` is parsed to
  resolve locator labels to concept names and flatten the tree hierarchy
  into a global display order.
- **CF_ENDC derivation**: ending-cash tag derived by stripping
  `PeriodIncreaseDecrease` from the CF root concept, with fallbacks for
  e.g. `...IncludingDisposalGroupAndDiscontinuedOperations`.
- **`__OTHER__` rows**: bottom-up insertion. For each branch node,
  `Other = declared_value − SUM(children * weight)`. Can be positive
  (missing items) or negative (overshoot).

### Stage 3 — Merge filings

**Entrypoint:** `merge.trees.merge_filing_trees(tree_files)`
**Output:** A single consolidated tree dict (same shape as a per-filing
tree) with `complete_periods` covering all years across all filings.

If only one filing is found, this stage is skipped and the per-filing
tree is used directly.

The merge takes filings in newest-first order and:

1. Uses the newest filing's tree as the **structural skeleton**.
2. Builds a `ConceptMap` (`merge.concepts.ConceptMatcher.align_statement`)
   that detects renamed concepts across adjacent filings by matching
   values across overlapping periods.
3. Fills values into the base tree by concept (`merge_values_by_concept`).
4. Detects **orphan concepts** (present in older filings but absent from
   the base tree) and only adds them under their parent if doing so
   *reduces* the children-vs-parent gap and never widens it for any
   period.
5. Detects and patches **structural reclassifications**
   (`detect_and_fix_structural_shifts`) where a line item moved between
   parents across filings.
6. Recomputes `__OTHER__` residuals so every parent still satisfies
   `SUM(children) == declared_value`.

### Stage 4 — Verify invariants (with optional LLM repair)

**Entrypoint:** `model.verify.run_checkpoint(merged) → CheckpointResult`

`verify_model()` runs **7 cross-statement checks**, all using `fv()` —
formula values, i.e. *what the sheet's `=SUM(...)` would produce* — so
the verification reflects what users will see, not just declared XBRL
values:

| # | Invariant | What it catches |
|---|-----------|-----------------|
| 1 | `BS_TA == BS_TL + BS_TE` | Balance sheet doesn't balance |
| 2 | `CF_ENDC == BS_CASH` | Cash flow ending cash ≠ balance sheet cash |
| 3 | `INC_NET (IS) == INC_NET (CF)` | Net income mismatch across statements |
| 4 | `IS_DA == CF_DA` | D&A mismatch (role-tag based) |
| 5 | `IS_SBC == CF_SBC` | SBC mismatch (role-tag based) |
| 6 | `CF_BEGC[t] == BS_CASH[t-1]` | Beginning cash ≠ prior period's ending cash |
| 7 | Segment sums (recursive) | Children must sum to parent for IS Revenue and IS COGS at every level |

Tolerance is `abs(delta) > 1.0` (one currency unit).

#### Soft vs. hard invariants

`run_checkpoint()` distinguishes soft from hard failures:

- **Soft** (NI Link, D&A Link, SBC Link): semantic role-mapping mistakes.
  These can plausibly be fixed by re-tagging — e.g. moving `INC_NET` to a
  different IS node. `run_checkpoint` calls `model.llm_fixer.fix_invariants`
  to attempt repair via `move_role` / `change_weight` ops, then re-runs
  `verify_model`.
- **Hard** (BS Balance, Cash Link, Cash Begin, Segment Sums): math or
  parser bugs. Role-shuffling can't repair these and would risk masking
  them, so the LLM fixer is **skipped**.

If any errors remain after repair, `run_pipeline.py` raises `RuntimeError`
and the pipeline halts before writing a sheet.

### Stage 5 — Write Google Sheet

**Entrypoint:** `sheets.write_sheets(trees, company) → (sheet_id, sheet_url)`

Creates a 4-tab Google Sheet via the `gws` CLI:

| Tab | Content |
|-----|---------|
| IS | Optional revenue segments header, then the IS body in cascade layout (Revenue first, NI last). When segments are present, the IS Revenue row references the segment total. |
| BS | Assets section + Liabilities & Equity section, totals at bottom |
| CF | Calc tree + cash-proof block: Beginning Cash (hard), Net Change (formula reference to CF_NETCH), Ending Cash (`=Begin + NetChange`) |
| Summary | Cross-statement check formulas referencing cells across tabs via `global_role_map`; should all evaluate to 0 |

**Formula construction:**
- Leaf nodes: hard values from XBRL facts.
- Branch nodes: `=SUM(children * weight)` via `_build_weight_formula()`.
- `__OTHER__` rows: hard values (the residual gap).
- Cross-statement checks: declarative entries in
  `xbrl.reconcile.CROSS_STATEMENT_CHECKS`, rendered by
  `sheets.renderers._write_summary_tab`. Checks with missing roles (e.g.
  D&A for companies without a separate D&A line) are silently skipped.
- Ending Cash: `=Beginning + Net Change` — formula, not hardcoded.
- Column layout: narrow gutters (cols A, B, D), wide label column (C),
  100px-wide data columns (E onwards).

`sheets.api.gws_share(sheet_id, email, role)` is re-exported by
`run_pipeline` and called by `web/app.py` to share the resulting sheet
with the user.

---

## 5. LLM Usage

**The LLM is invoked from exactly one place: `model/llm_fixer.py`,
during invariant verification.** Stages 1, 2, 3, and 5 are fully
deterministic.

Flow:
1. `verify_model()` finds errors.
2. If *all* errors are soft (NI / D&A / SBC links), `run_checkpoint`
   imports `fix_invariants`, which:
   - Prunes each tree into a compact JSON representation
     (`_prune_tree_for_llm`).
   - Builds a prompt with the tree context, the failed invariants, and
     the affected periods, then calls `call_llm` (`llm.client`).
   - Expects a JSON list of operations: `{op: "move_role", role, new_concept}`
     or `{op: "change_weight", parent_concept, child_concept, weight}`.
   - Applies operations to the trees in-place.
3. `verify_model()` re-runs; if still failing, the pipeline halts.

The LLM cannot edit values, add nodes, or change statement structure —
only re-tag roles or flip child weights. This bounds the blast radius of
LLM hallucinations.

If `LLM_API_KEY` is missing, `validate_environment()` warns and
invariant repair is unavailable; everything else still runs.

---

## 6. Testing

### Test files

Unit and integration tests live under `tests/`:

| File | What it tests |
|------|---------------|
| `tests/test_dual_linkbase.py` | Presentation parsing, cascade layout, IS tagging, orphan supplementation, tree completeness, `CROSS_STATEMENT_CHECKS` |
| `tests/test_model_historical.py` | `verify_model()` on synthetic tree fixtures |
| `tests/test_offline_e2e.py` | End-to-end pipeline on cached filings |
| `tests/test_merge_pipeline.py` | Multi-filing merge end-to-end |
| `tests/test_merge_layers.py` | Three-layer merge algorithm (synthetic + real fixtures) |
| `tests/test_reclassification.py` | Structural-shift detection in `merge.concepts` |
| `tests/test_da_sbc_tagging.py` | D&A and SBC cross-statement tagging |
| `tests/test_bs_cash_fix.py` | BS_CASH override semantics |
| `tests/test_pymodel_units.py` | Unit handling (millions vs thousands, etc.) |
| `tests/test_sheet_formulas.py` | `_build_weight_formula()`, `dcol()` |
| `tests/test_share_sheet*.py` | `gws_share()` integration & unit tests |
| `tests/test_demo_website.py` | `web/app.py` smoke tests |

### Test fixtures

10 company fixtures under `tests/fixtures/sec_filings/`: AAPL, AMZN,
BRK-B, GOOG, JPM, META, MSFT, NFLX, PFE, TSLA. Each has cached filing
HTML, `_cal.xml`, `_pre.xml`, and pre-built `trees.json`.

### Running tests

```bash
python -m pytest tests/ -v                         # all tests
python -m pytest tests/test_dual_linkbase.py -v    # one file
python run_pipeline.py AAPL                        # live end-to-end
```

---

## 7. Tested Companies & Known Edge Cases

### Scorecard summary

Tested on 10 companies across 6 industries. **9 / 10 ALL PASS**; PFE has
a small (~$401) rounding error from BS root selection.

### Known edge cases

1. **MSFT** — uses XBRL Calculation 1.1 (`calculation-1.1.xsd`) instead
   of traditional `_cal.xml`. The current regex-based `fetch_cal_linkbase`
   doesn't match it. Requires a parser update (Phase 5 below).
2. **GE** — NI Link gap because IS reports "Income from Continuing
   Operations" while CF reports `ProfitLoss` (includes discontinued
   operations). Real data difference, not a bug.
3. **TSLA** — Cash Link shows ~$900 difference because `BS_CASH` uses
   `CashAndCashEquivalentsAtCarryingValue` (excludes restricted cash)
   while `CF_ENDC` uses
   `...IncludingDisposalGroupAndDiscontinuedOperations` (includes it).
4. **Dimensional data** — companies like AAPL report Products / Services
   revenue split via XBRL dimensions (`ProductOrServiceAxis`), not as
   separate concepts. The revenue-segment tree handles the common cases;
   uncommon dimension axes are a future enhancement.

---

## 8. Roadmap

### Done

- **Phase 1** — XBRL tree engine: deterministic extraction from iXBRL +
  calc linkbase, position-based tagging, cross-statement invariants.
- **Phase 1b** — Dual-linkbase + three-layer merge: presentation parsing,
  cascade rendering, `__OTHER__` rows, declarative cross-statement checks.
- **Phase 2** — Decoupled sheet builder: `sheets/` package extracted from
  the verification module. Weight-aware formulas, summary tab.
- **Phase 3** — Dynamic sheet formulas: all subtotals are `=SUM()`,
  cross-sheet references via `global_role_map`, formula-derived Ending
  Cash, formula-based check rows.
- **Phase 3b** — Multi-filing merge with concept alignment, orphan
  insertion, structural-shift repair.
- **Phase 4a** — LLM-in-the-loop soft-invariant repair
  (`model/llm_fixer.py`).

### Future

- **Forecast layer** — LLM reads MD&A + historical baseline →
  `forecast_spec.json` (business drivers only, no math); `model/`
  applies drivers to compute forward periods; `sheets/` renders forecast
  columns with driver formulas; inline assertions after each step for
  immediate failure localisation.
- **XBRL Calculation 1.1 support** — handle the new linkbase format used
  by MSFT and other recent filers.
- **Richer dimensional segments** — full multi-axis breakdowns
  (geography × product, etc.) beyond the current revenue-segment tree.
