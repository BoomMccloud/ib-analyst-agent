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

It must **not** import `sec_utils`, `xbrl_tree`, `sheet_builder`, `gws_utils`, or any other pipeline internal. This is the line that keeps business logic out of the web layer.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET /` | — | Serves `static/index.html`. |
| `GET /api/search?q=appl` | — | Returns `[{ticker, name, cik, filing_type}]`. Substring match on cached `company_tickers.json`. |
| `POST /api/jobs` body `{ticker, years?}` | — | Starts pipeline, returns `{job_id}`. **Idempotent on same ticker:** if the currently running job is for the same ticker, returns its existing `job_id`. If a *different* ticker is running, returns 409. |
| `GET /api/jobs/{id}` | — | Returns `{status, stage, log, sheet_url?, error?}`. Status ∈ `running\|done\|error`. 404 if id unknown. |

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
   - Loads `company_tickers.json` once, caches in module global.
   - Case-insensitive substring match on both `ticker` and `title`.
   - Returns list of dicts; rank exact-ticker matches first.
   - Leave existing single-result function untouched.

2. **`run_pipeline.py`** — extract a function:
   ```python
   def run_pipeline(query, years=5, outdir="./pipeline_output",
                    on_progress=None) -> dict:
       # returns {"sheet_url": ..., "company_name": ...}
   ```
   - Replace `sys.exit(...)` with `raise RuntimeError(...)`. No custom exception class.
   - Call `on_progress(stage_name, message)` at each stage boundary. Default to a no-op if `None`.
   - Keep `main()` as thin CLI wrapper that prints + exits.
   - Capture sheet URL from `sheet_builder.write_sheets()` (already returns `(sid, url)`) and return it.

## Frontend (`index.html`)

Single page, three visual states swapped via JS:

1. **Search:** input box, debounced 250ms, `GET /api/search`, render results as clickable rows showing `TICKER — Company Name (10-K/20-F)`.
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
uvicorn web.app:app --reload
# then open http://localhost:8000
```

## Build order

1. Refactor `lookup_company.search_tickers()` + manual test (`python -c "from lookup_company import search_tickers; print(search_tickers('appl'))"`).
2. Refactor `run_pipeline.run_pipeline()` as importable function + verify CLI still works on AAPL.
3. Write `web/app.py` from the skeleton above. Smoke-test each endpoint with `curl`.
4. Write `static/index.html`. Test full flow end-to-end.
5. Update README.

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
