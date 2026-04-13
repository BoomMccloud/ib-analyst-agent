# SEC Modeler Demo Website — Plan

Local-only demo wrapping the existing 4-stage pipeline in a browser UI.

## Goal

Browser flow on `localhost:8000`:
1. User types a ticker or company name.
2. Sees a list of matches from SEC EDGAR.
3. Clicks one → pipeline runs → returns a Google Sheet link.

## Constraints / scope decisions

- **Localhost only.** Single user (you), running during the demo.
- **Auth:** uses your existing `gws` CLI OAuth as-is. Sheets land in your own Drive.
- **Concurrency:** one pipeline job at a time. No queue, no DB.
- **No Docker, no deploy packaging.** Just `uvicorn`.

## Architecture

```
sec-agent/
  web/
    app.py            # FastAPI app — entire backend, ~100 lines
    static/
      index.html      # single page, vanilla JS
```

Two files. The backend is one file. State lives in a module-level dict guarded by a lock. No class, no separate `jobs.py`, no dependency injection.

### Driver boundary (hard rule)

`web/app.py` may import **only**:
- `run_pipeline` from `run_pipeline.py`
- `search_tickers` from `lookup_company.py`

It must **not** import anything from the `xbrl/` or `sheets/` packages, nor `merge_trees`, `concept_matcher`, `pymodel`, `parse_xbrl_facts`, `gws_utils`, `sec_utils`, `llm_utils`, `llm_invariant_fixer`, or any other pipeline internal. This is the line that keeps business logic out of the web layer.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET /` | — | Serves `static/index.html`. |
| `GET /api/search?q=appl` | — | Returns `[{ticker, name, cik}]`. Substring match on in-memory cache of SEC `company_tickers.json` (fetched on first request). |
| `POST /api/jobs` body `{ticker, years?}` | — | Starts pipeline, returns `{job_id}`. **Idempotent on same ticker:** if the currently running job is for the same ticker, returns its existing `job_id`. If a *different* ticker is running, returns 409. |
| `GET /api/jobs/{id}` | — | Returns `{status, stage, log, sheet_url?, error?}`. Status ∈ `running\|done\|error`. `error` is a single human-readable string (e.g. `"verify_model: BS_TA != BS_TL + BS_TE for 2024-12-31 (gap=$2,341)"` or `"gws auth expired — re-run 'gws auth login'"`); the worker formats it from the exception message + stage name. 404 if id unknown. |

## Backend skeleton (entire `web/app.py`)

```python
import threading, uuid, traceback
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from run_pipeline import run_pipeline
from lookup_company import search_tickers

app = FastAPI()
app.mount("/", StaticFiles(directory="web/static", html=True), name="static")

_lock = threading.Lock()
_state = {
    "id": None, "ticker": None, "status": "idle",
    "stage": "", "log": [], "sheet_url": None, "error": None,
}

def _update(stage, msg):
    with _lock:
        _state["stage"] = stage
        _state["log"] = (_state["log"] + [msg])[-20:]

def _worker(job_id, ticker, years):
    try:
        result = run_pipeline(ticker, years, on_progress=_update)
        with _lock:
            if _state["id"] == job_id:
                _state.update(status="done", sheet_url=result["sheet_url"])
    except Exception as e:
        with _lock:
            if _state["id"] == job_id:
                _state.update(status="error",
                              error=f"{e}\n{traceback.format_exc()}")

@app.get("/api/search")
def search(q: str):
    return search_tickers(q, limit=10)

@app.post("/api/jobs")
def start_job(body: dict):
    ticker = body["ticker"]
    years = body.get("years", 5)
    with _lock:
        if _state["status"] == "running":
            if _state["ticker"] == ticker:
                return {"job_id": _state["id"]}        # idempotent re-click
            raise HTTPException(409, "another job is running")
        jid = uuid.uuid4().hex
        _state.update(id=jid, ticker=ticker, status="running",
                      stage="starting", log=[], sheet_url=None, error=None)
    threading.Thread(target=_worker, args=(jid, ticker, years), daemon=True).start()
    return {"job_id": jid}

@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    with _lock:
        if _state["id"] != jid:
            raise HTTPException(404)
        return dict(_state)
```

That's the whole backend. Properties it satisfies:

- **Single source of truth:** `_state` dict.
- **Intent-driven:** POST = command, GET = read. No reactive cascades.
- **One driver:** `run_pipeline`. Web layer knows nothing about SEC, XBRL, or `gws`.
- **Bulletproof termination:** worker `try/except` always lands the job in `done` or `error`. Bare `Exception` catches everything.
- **Idempotent spam-click:** same ticker → same `job_id`.
- **No leaked slot:** next `start_job` overwrites `_state` wholesale, so no "stuck running" state can wedge the app.

## Files to touch

### New

- `sec-agent/web/app.py` — code above.
- `sec-agent/web/static/index.html` — UI.

### Refactor (minimal)

1. **`lookup_company.py`** — add `search_tickers(query, limit=10)`:
   - **Data source:** there is no local `company_tickers.json` — `lookup_by_ticker` calls `fetch_json(TICKERS_URL)` (SEC EDGAR) on every call. `search_tickers` should fetch it **once** via `fetch_json(TICKERS_URL)`, store the parsed dict in a module global, and reuse it. First call pays a ~1s SEC HTTP round-trip (rate-limited to 8 req/s); all subsequent calls are in-memory.
   - Case-insensitive substring match on both `ticker` and `title`.
   - Returns list of `{ticker, name, cik}` dicts; rank exact-ticker matches first. **Note:** `company_tickers.json` does *not* contain filing type — drop `filing_type` from the search result shape. (See "Frontend" section below — the result row will show `TICKER — Company Name`, no parenthetical.) If filing type matters, it gets resolved at job-start time from `lookup_by_name`/`get_filer_info`, not at search time.
   - Leave existing single-result functions (`lookup_by_ticker`, `lookup_by_name`) untouched.

2. **`run_pipeline.py`** — **rewrite** as an in-process function (currently it's entirely subprocess-driven, shelling out to `agent1_fetcher.py`, `xbrl_tree.py`, `merge_trees.py`, `pymodel.py`, `sheet_builder.py`).

   *Decision: in-process vs subprocess.* The naive alternative is to keep the subprocess pipeline and grep the sheet URL from stdout. We're choosing the in-process rewrite because (a) the architecture refactor already exposed clean `xbrl/` and `sheets/` import surfaces, (b) per-stage `on_progress` callbacks give materially better demo UX than tailing stdout, and (c) it sidesteps a real working-directory bug — the current subprocess calls use bare relative paths (`"agent1_fetcher.py"`), which break when FastAPI is launched from a different cwd. If the in-process path balloons, fall back to subprocess but pin `cwd=` to the `sec-agent/` directory.

    ```python
    def run_pipeline(query, years=5, outdir="./pipeline_output",
                     on_progress=None) -> dict:
        # returns {"sheet_url": ..., "company_name": ...}
    ```
    - **Imports it can use directly:** `agent1_fetcher.run(query, years) -> dict` already exists (line 26) — use it as-is, no refactor needed. `xbrl.*`, `sheets.write_sheets`, `merge_trees.merge_filing_trees`, `pymodel.verify_model`. Verified post-refactor: `sheets.write_sheets()` returns `(sid, url)` at `sheets/__init__.py:169`. ✅
    - **`pymodel.py` needs a non-exiting variant.** Today `pymodel.main()` calls `sys.exit(1)` (line 173) on invariant failure, and on the way to that it imports `llm_invariant_fixer.fix_invariants` (line 160) — a Sonnet call that can take many seconds. Extract a `run_checkpoint(trees_data) -> CheckpointResult` function that returns a structured result instead of exiting. Keep `main()` as the CLI wrapper that translates the result to `sys.exit`. The LLM-fixer call is fine inside the demo's worker thread (it's not on the FastAPI event loop) but the `on_progress` updates around it should make clear the user is waiting on an LLM round-trip.
    - **Other `sys.exit` callsites to leave alone:** `lookup_company.py:33` (env var validation at import) is acceptable — if `SEC_CONTACT_EMAIL` is unset the demo can't run anyway, fail-fast at import is correct. `lookup_company.py:162` is in `main()` of that script, not reached from imports.
    - Replace `sys.exit(...)` *inside* `run_pipeline` itself with `raise RuntimeError(...)`. No custom exception class.
    - Call `on_progress(stage_name, message)` at each stage boundary. Default to a no-op if `None`.
    - Keep `main()` as a thin CLI wrapper that prints + exits.
    - Capture sheet URL by calling `sheets.write_sheets()` directly in-process and returning the `(sid, url)` it produces.

    **Internal flow** (what `run_pipeline()` does step-by-step):
    1. Call `agent1_fetcher.run(query, years)` → get `{filings, company, ticker, cik, filing_type}`. If empty filings, `raise RuntimeError("No filings found for {query}")`.
    2. For each filing URL, call `xbrl_tree.build_statement_trees(html, base_url)` → `{IS, BS, BS_LE, CF, complete_periods, cf_endc_values, ...}`. Write each to `outdir/trees_<date>.json`. Collect tree file paths.
    3. If multiple trees: call `merge_trees.merge_filing_trees(tree_files)` → merged dict. Write to `outdir/merged.json`. If single tree: use it directly.
    4. Call `pymodel.verify_model(trees_data)` → list of errors. If errors, the existing `pymodel.main()` flow calls `llm_invariant_fixer.fix_invariants()` — wrap this in `run_checkpoint()` so it returns a structured result instead of `sys.exit`. If still failing after LLM fix, `raise RuntimeError("verify_model: {first_error}")`.
    5. Call `sheets.write_sheets(trees, company_name)` → `(sid, url)`. Return `{"sheet_url": url, "company_name": company_name}`.

## Frontend (`index.html`)

Single page, three visual states swapped via JS:

1. **Search:** input box, debounced 250ms, `GET /api/search`, render results as clickable rows showing `TICKER — Company Name`. (No filing-type badge — `company_tickers.json` doesn't carry it, and resolving it per-row would mean an SEC API call per result.)
2. **Running:** shows selected company, current stage name, last ~10 log lines. Polls `/api/jobs/{id}` every 2s.
3. **Done:** big "Open Google Sheet ↗" button linking to `sheet_url`. "Run another" resets to state 1.

No framework, no build, no localStorage. Plain CSS. Reload mid-job loses your view — fine for a controlled demo.

## Dependencies

```
pip install fastapi uvicorn[standard]
```

Document in README under a new "Demo website" section.

## Run command

```bash
cd sec-agent
export SEC_CONTACT_EMAIL="you@example.com"   # required by lookup_company.py
gws auth login                                # ensure Sheets OAuth is fresh
uvicorn web.app:app --reload
# then open http://localhost:8000
```

**Pre-flight checklist** — these will bite you mid-demo if forgotten:
- `SEC_CONTACT_EMAIL` env var set (lookup_company.py:33 hard-fails at import without it).
- `gws` CLI authenticated and token not expired (otherwise stage 4 dies inside `sheets.write_sheets`, surfaces as a `gws auth` error in the job's `error` field — no automatic recovery).
- Run from `sec-agent/` directory (the in-process rewrite removes the old relative-path subprocess hazard, but `outdir="./pipeline_output"` is still cwd-relative).

## Build order

**Where the work actually is:** Step 3 is ~80% of the effort. Steps 1, 2, 4, 5, 6 are each well under an hour. If step 3 starts to balloon, that's the signal to reconsider the in-process decision above and fall back to subprocess + stdout-grepping.

1. Add `lookup_company.search_tickers()` + manual test (`python -c "from lookup_company import search_tickers; print(search_tickers('appl'))"`).
2. Verify `sheets.write_sheets()` return signature (`(sid, url)`) post-refactor — one-line check before relying on it.
3. **(Bulk of the work.)** Rewrite `run_pipeline.py` as in-process `run_pipeline()` using `xbrl/`, `sheets/`, `merge_trees`, `pymodel` imports. Wire `on_progress` callback. Keep `main()` CLI wrapper. Verify CLI still works on AAPL end-to-end.
4. Write `web/app.py` from the skeleton above. Smoke-test each endpoint with `curl`.
5. Write `static/index.html`. Test full flow end-to-end.
6. Update README.

## Out of scope for v1

- Cache reuse across runs. Add only if you'll actually demo the same ticker twice — say so and I'll add it.
- Multiple concurrent jobs.
- Persistent job history across server restarts (in-memory only — `ctrl-C` loses state).
- Authentication / multi-user.
- Cancel button.
- Reload-mid-job recovery (`localStorage`).
- Forecasting UI (Phase 4 of pipeline isn't built yet).
- Deployment, Docker/podman packaging.
- Streaming logs via SSE/WebSocket (polling is fine for a demo).
- Web-layer test suite (use `curl` for smoke tests).
