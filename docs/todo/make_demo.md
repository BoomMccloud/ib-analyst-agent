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
- **Cache reuse:** if `pipeline_output/merged_<TICKER>.json` already exists, skip fetch/extract and jump to `sheet_builder` so demo reruns are instant.
- **No Docker, no deploy packaging.** Just `uvicorn`.

## Architecture

```
sec-agent/
  web/
    app.py            # FastAPI app
    jobs.py           # in-memory job registry
    static/
      index.html      # single page, vanilla JS
```

- **Backend:** FastAPI. Imports existing pipeline modules directly (no subprocess).
- **Frontend:** one static HTML page, vanilla JS, no build step.
- **Job runner:** in-process dict + `asyncio.create_task` running the sync pipeline in `run_in_executor`. Progress callback updates the dict.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET /` | — | Serves `static/index.html`. |
| `GET /api/search?q=appl` | — | Returns `[{ticker, name, cik, filing_type}]`. Substring match on cached `company_tickers.json`. |
| `POST /api/jobs` body `{ticker, years}` | — | Starts pipeline, returns `{job_id}`. Rejects with 409 if a job is already running. |
| `GET /api/jobs/{id}` | — | Returns `{status, stage, log_tail, sheet_url?, error?}`. Status ∈ `queued\|running\|done\|error`. |

## Files to touch

### New

- `sec-agent/web/app.py` — FastAPI app, endpoints above.
- `sec-agent/web/jobs.py` — `JobRegistry` class: `start(fn)`, `get(id)`, single-slot lock.
- `sec-agent/web/static/index.html` — UI (search → results → progress → sheet link).

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
   - Replace `sys.exit(...)` with raised exceptions (`PipelineError`).
   - Call `on_progress(stage_name, message)` at each stage boundary.
   - Keep `main()` as thin CLI wrapper that prints + exits.
   - Capture sheet URL from `sheet_builder` instead of just letting it print.

3. **`sheet_builder.py`** — already returns `(sid, url)` from `write_sheets`. Just make sure `run_pipeline` consumes it. Likely a one-liner change in `run_pipeline.py`.

### Cache-reuse hook

In `run_pipeline()`, before stage 1:
```python
merged = Path(outdir) / f"merged_{ticker}.json"
if merged.exists():
    on_progress("cache", f"Reusing cached merged tree for {ticker}")
    # jump straight to stage 4
```

(Currently `run_pipeline.py` writes `merged.json` — change to `merged_<TICKER>.json` so cache is per-company.)

## Frontend (`index.html`)

Single page, three visual states swapped via JS:

1. **Search:** input box, debounced 250ms, `GET /api/search`, render results as clickable rows showing `TICKER — Company Name (10-K/20-F)`.
2. **Running:** shows selected company, current stage name, last ~10 log lines. Polls `/api/jobs/{id}` every 2s.
3. **Done:** big "Open Google Sheet ↗" button linking to `sheet_url`. "Run another" resets to state 1.

No framework. ~150 lines total. Tailwind via CDN if styling matters for the demo, otherwise plain CSS.

## Dependencies

Add to whatever dep mechanism the repo uses (likely just `pip install`):
- `fastapi`
- `uvicorn[standard]`

Document in README under a new "Demo website" section.

## Run command

```bash
cd sec-agent
uvicorn web.app:app --reload
# then open http://localhost:8000
```

## Build order

1. Refactor `lookup_company.search_tickers()` + quick manual test.
2. Refactor `run_pipeline.run_pipeline()` as importable function + verify CLI still works on AAPL.
3. Add per-ticker cache filename.
4. Build `web/jobs.py` (single-slot job registry).
5. Build `web/app.py` with all 4 endpoints. Test each with `curl`.
6. Build `static/index.html`. Test full flow end-to-end with a cached ticker (instant) and a cold ticker (full pipeline).
7. Update README with run instructions.

## Out of scope for v1

- Multiple concurrent jobs.
- Persistent job history across server restarts.
- Authentication / multi-user.
- Forecasting UI (Phase 4 of pipeline isn't built yet).
- Deployment, Docker/podman packaging.
- Streaming logs via SSE/WebSocket (polling is fine for a demo).
