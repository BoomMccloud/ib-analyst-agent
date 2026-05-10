VERIFY_STATUS: WARNINGS

# Spec Verification Report — Share Sheet by Email

**Spec**: `docs/todo/spec_share_sheet.md`
**Verified**: 2026-05-10
**Overall Status**: WARNINGS

## Primitives Verified

| # | Stage 1 Claim | Actual | Match? |
|---|---------------|--------|--------|
| 1 | `web/app.py:17-25` — `_state` dict | Lines 17–25 define `_state = {"id", "ticker", "status", "stage", "log", "sheet_url", "error"}` | YES |
| 2 | `web/app.py:34-43` — `_worker` | Lines 34–43 define `def _worker(job_id, ticker, years)` calling `run_pipeline` and updating state on success/error | YES |
| 3 | `web/app.py:74-79` — `get_job` | Lines 74–79 define `@app.get("/api/jobs/{jid}") def get_job(jid: str)` returning `dict(_state)` or 404 | YES |
| 4 | `web/app.py:84` — StaticFiles mount | Line 84: `app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")` | YES |
| 5 | `sheets/api.py:4-11` — `gws_create` | Lines 4–11 define `def gws_create(title, sheet_names)` returning `(sid, url, sheet_ids)` | YES |
| 6 | `sheets/gws.py:12-22` — `_run_gws` | Lines 12–22 define `def _run_gws(*args) -> dict` running `["gws", *args]` via subprocess, returning parsed JSON | YES |
| 7 | `run_pipeline.py:118` — `sid, url = write_sheets(...)` | Line 118: `sid, url = write_sheets(merged, company_name)` | YES |
| 8 | `run_pipeline.py:121` — return dict `{sheet_url, company_name}` | Line 121: `return {"sheet_url": url, "company_name": company_name}` (no `sheet_id` today — spec adds it) | YES |
| 9 | `web/static/index.html:206-218` — `done-view` block | Lines 206–218 contain the `<div id="done-view" class="hidden">` block with `#sheet-link` and `#run-another` button | YES |
| 10 | `web/static/index.html:246-254` — `resetToSearch()` | Lines 246–254 define `function resetToSearch()` clearing `pollTimer`, `currentJobId`, `selectedTicker`, `#search-input`, `#results` | YES |
| 11 | `web/static/index.html:313-318` — done branch of `pollJob` | Lines 313–318: `if (job.status === "done")` clears `pollTimer`, sets `#done-company`, `#sheet-link.href`, calls `showView("done-view")` | YES |

## Notes

### WARN-001 — Driver boundary test forbids importing from `sheets` in `web/app.py`

`tests/test_demo_website.py::test_app_py_driver_boundary` (lines 330–350) asserts that `web/app.py` must not contain `import sheets` or `from sheets`. The forbidden list includes the literal string `"sheets"` (matched as `"import sheets"` and `"from sheets"`).

The spec proposes calling `gws_share(sid, email)` from inside the new `/api/share` route in `web/app.py`. As written, the natural `from sheets.api import gws_share` would FAIL this existing test.

**Recommended fix (Stage 5/6 must address)**:
- Option A (preferred): expose `gws_share` via `run_pipeline.py` (which already imports from `sheets`) — e.g. add `from sheets.api import gws_share` to `run_pipeline.py` and re-export, then `from run_pipeline import gws_share` in `web/app.py`. This matches the existing driver boundary (web/app.py only imports from `run_pipeline` and `fetch.lookup`).
- Option B: update `test_app_py_driver_boundary` to permit `from sheets.api import gws_share` specifically. This weakens the boundary invariant; the spec author should approve.

This is non-blocking because the spec is implementable, but Stage 4 (TDD) and Stage 5 (Guide) MUST decide which option to take or the existing test will fail.

### WARN-002 — `gws_share` location

Spec puts `gws_share` in `sheets/api.py`. `sheets/api.py` currently only contains `gws_create` and imports `_run_gws` from `sheets.gws`. The pattern fits, but most other `_run_gws` callers live in `sheets/gws.py` itself (`gws_write`, `gws_batch_update`). Either location works; the spec's choice (`sheets/api.py`) is consistent with `gws_create`. No change needed.

### Regex check (Spec Item 3)

Pattern: `^[^@\s]+@[^@\s]+\.[^@\s]+$`

In Python `re`, this is valid and behaves as intended:
- `^` / `$`: anchors
- `[^@\s]+`: one or more non-`@`, non-whitespace chars
- Literal `@` and `.` (the `.` inside `[...]` is literal, no need to escape)

No escaping issues. Confirmed by inspection that `\s` is the standard whitespace metacharacter and `\.` outside the character class is properly escaped. Compiled with `re.compile(r"...")` (raw string), the backslashes are passed through correctly.

This regex matches both client and server, satisfying spec acceptance criterion 3 (rejects `"foo"`, `""`, `"a@b"` — note `"a@b"` lacks the `.<tld>` segment so will be rejected; `""` is rejected because `+` requires ≥1 char).

### `gws drive permissions create` argv shape (Spec Item 4)

Existing `_run_gws` invocations in the codebase:
- `sheets/api.py:6-7` — `_run_gws("sheets", "spreadsheets", "create", "--json", json.dumps({...}))`
- `sheets/gws.py:35` — `_run_gws("sheets", "spreadsheets", "values", "update", "--params", params, "--json", body)`
- `sheets/gws.py:47` — `_run_gws("sheets", "spreadsheets", "batchUpdate", "--params", params, "--json", body)`

The spec's proposed `_run_gws("drive", "permissions", "create", "--params", params, "--json", body)` follows the identical pattern (positional verb tokens + `--params` + `--json` flags with JSON-encoded strings). Argv shape is consistent.

Note: `_run_gws` returns `{}` if stdout is empty. `gws drive permissions create` typically returns the created Permission resource as JSON; either way the wrapper handles both cases. Spec correctly types `gws_share` return as `None`.

### `from sheets.gws import _run_gws` (Spec Item 5)

Confirmed. `sheets/api.py:2` already uses exactly this import: `from sheets.gws import _run_gws`. The spec's `gws_share` (placed in `sheets/api.py`) reuses the same imported symbol — no new import needed.

### `sheets/__init__.py` re-exports (Spec Item 6)

`sheets/__init__.py` is **empty** (0 bytes — only the marker file exists). No symbols are re-exported. Existing callers always use the fully qualified path:
- `from sheets.api import gws_create` (e.g. `sheets/builder.py:3`)
- `from sheets.gws import gws_batch_update` (e.g. `sheets/builder.py:2`, `tests/test_demo_website.py:2`)

**Implication**: No need to add `gws_share` to `sheets/__init__.py`. Callers should `from sheets.api import gws_share`. This matches the existing convention.

### Additional observations

- `currentJobId` is already declared as a module-level JS variable (line 237) — the new share handler can reference it without re-declaration.
- `done-view` is hidden via the existing `showView()` toggler; new fieldset embedded inside `done-view` will inherit hide/show behavior automatically.
- The fieldset/legend HTML pattern matches the existing 98.css aesthetic used elsewhere in `index.html` (e.g. line 200's "MS-DOS Prompt" fieldset).

## Test Conventions (from `tests/test_demo_website.py`)

Stage 4 (TDD) should follow these patterns:

1. **Imports & path setup** (lines 12–23):
   ```python
   import os, sys, threading, time
   from pathlib import Path
   import pytest
   SEC_AGENT_ROOT = Path(__file__).resolve().parent.parent
   if str(SEC_AGENT_ROOT) not in sys.path:
       sys.path.insert(0, str(SEC_AGENT_ROOT))
   os.environ.setdefault("SEC_CONTACT_EMAIL", "test@example.com")
   ```

2. **FastAPI testing**: Use `from fastapi.testclient import TestClient` with `TestClient(webapp.app)`.

3. **`app_client` fixture pattern** (lines 145–173):
   - Resets `webapp._state` under `webapp._lock` to clean defaults at start of each test.
   - Stubs `run_pipeline` via `monkeypatch.setattr(webapp, "run_pipeline", fake_pipeline)` — fake returns `{"sheet_url": ..., "company_name": ...}` synchronously.
   - Stubs `search_tickers` similarly.
   - Returns `(client, webapp)` tuple so tests can both call HTTP and inspect/mutate module state.

4. **Background-thread synchronization** (line 176–182): `_wait_until(predicate, timeout=3.0, interval=0.02)` polls `webapp._state` rather than `time.sleep` with a fixed delay. Use `threading.Event` (`started`, `release`) to gate slow stub pipelines.

5. **Error-path tests** (lines 269–286): Stub `run_pipeline` to `raise RuntimeError(...)`; assert `webapp._state["status"] == "error"` and the error message propagates into the GET response body.

6. **No real network/subprocess**: All external calls (SEC EDGAR, `gws` CLI) are stubbed via monkeypatch. For `gws_share`, Stage 4 should monkeypatch `web.app.gws_share` (or the import path it ends up using) — never invoke the real `gws` CLI.

7. **Driver boundary test** (lines 330–350): Stage 4 should add a similar regex/string check that the `/api/share` route exists and that the way `gws_share` is imported into `web/app.py` does NOT violate the existing forbidden-import list. See WARN-001 for the resolution paths.

8. **Naming**: Test files use `test_<feature>.py`; functions use `test_<scenario>_<expected>` snake_case. New tests should land in `tests/test_share_sheet.py` (matches `<project-config>` `run-single-test`).

VERIFY_STATUS: WARNINGS
